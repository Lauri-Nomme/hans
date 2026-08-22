//! bb-obj-tar: stream a bare git repo's pack into a Bitbucket-format
//! loose-object tar, byte-identical to `git unpack-objects` output.
//!
//! A loose git object file is exactly:
//!     zlib(level=1) of  "<type> <size>\0" + object-content
//! (verified byte-identical to `git unpack-objects` on 54/54 golden objects).
//! So we decompress each object once (via `git cat-file --batch`), re-compress
//! it at zlib level 1 in-process, and write USTAR entries named `<ab>/<rest>`
//! (mode 0400, mtime 0) — without ever creating one file per object on disk.
//!
//! Output: `--chunks N` part files (no end-of-archive marker), which are then
//! concatenated + a 1024-byte zero block appended to form the final
//! `objects.atl.tar`. Parts are independently streamable and joinable, so the
//! work parallelizes across N threads and can be resumed/joined later.

use std::fs::File;
use std::io::{BufRead, BufReader, BufWriter, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};

use flate2::write::ZlibEncoder;
use flate2::Compression;

/// Parse a pack `.idx` (v2 only) and return all object shas (hex) in pack order.
fn idx_shas(idx_path: &Path) -> Result<Vec<String>, String> {
    let data = std::fs::read(idx_path).map_err(|e| format!("read {}: {e}", idx_path.display()))?;
    if data.len() < 8 || &data[0..4] != b"\xfftOc" {
        return Err(format!("{}: not a v2 pack idx", idx_path.display()));
    }
    let ver = u32::from_be_bytes([data[4], data[5], data[6], data[7]]);
    if ver != 2 {
        return Err(format!("{}: unexpected idx version {ver}", idx_path.display()));
    }
    let n = u32::from_be_bytes([data[8 + 255 * 4], data[9 + 255 * 4], data[10 + 255 * 4], data[11 + 255 * 4]]) as usize;
    let names_off = 8 + 256 * 4; // magic+ver+fanout
    let names = &data[names_off..names_off + n * 20];
    let mut out = Vec::with_capacity(n);
    for i in 0..n {
        let o = i * 20;
        out.push(hex(&names[o..o + 20]));
    }
    Ok(out)
}

fn hex(b: &[u8]) -> String {
    const H: &[u8; 16] = b"0123456789abcdef";
    let mut s = String::with_capacity(b.len() * 2);
    for &x in b {
        s.push(H[(x >> 4) as usize] as char);
        s.push(H[(x & 0xf) as usize] as char);
    }
    s
}

/// USTAR entry for one loose object, byte-format matching Python tarfile PAX.
fn tar_entry(name: &str, data: &[u8]) -> Vec<u8> {
    let mut h = [0u8; 512];
    let nameb = name.as_bytes();
    h[0..nameb.len()].copy_from_slice(nameb);
    h[100..107].copy_from_slice(b"0000400");
    h[107] = 0;
    h[108..115].copy_from_slice(b"0000000");
    h[115] = 0;
    h[116..123].copy_from_slice(b"0000000");
    h[123] = 0;
    let size_oct = format!("{:011o}", data.len());
    h[124..135].copy_from_slice(size_oct.as_bytes());
    h[135] = 0;
    h[136..147].copy_from_slice(b"00000000000");
    h[147] = 0;
    // checksum field left as spaces for now
    h[148..156].copy_from_slice(b"        ");
    h[156] = b'0'; // typeflag
    h[257..263].copy_from_slice(b"ustar\0");
    h[263..265].copy_from_slice(b"00");
    // uname/gname empty (zeros), devmajor/minor zeros, prefix empty

    let sum: u64 = h.iter().map(|&b| b as u64).sum();
    let cks = format!("{:06o}\0 ", sum);
    h[148..156].copy_from_slice(cks.as_bytes());

    let mut out = Vec::with_capacity(512 + data.len() + 511);
    out.extend_from_slice(&h);
    out.extend_from_slice(data);
    // pad to 512
    let rem = data.len() % 512;
    if rem != 0 {
        out.resize(out.len() + (512 - rem), 0);
    }
    out
}

fn zlib_level1(raw: &[u8]) -> Vec<u8> {
    let mut enc = ZlibEncoder::new(Vec::with_capacity(raw.len() + 64), Compression::new(1));
    let _ = enc.write_all(raw);
    enc.finish().unwrap()
}

struct Worker {
    shas: Vec<String>,
    gitdir: PathBuf,
    part_path: PathBuf,
    done: Arc<AtomicU64>,
}

fn run_worker(w: Worker) -> Result<(), String> {
    let mut child = Command::new("git")
        .arg("-C")
        .arg(&w.gitdir)
        .arg("cat-file")
        .arg("--batch")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .map_err(|e| format!("spawn git cat-file: {e}"))?;
    // Strictly interleave: write one sha, flush, read its response. This is the
    // batch protocol's contract and cannot deadlock on the pipes.
    let mut stdin = BufWriter::new(child.stdin.take().ok_or("no stdin")?);
    let stdout = child.stdout.take().ok_or("no stdout")?;
    let mut out = BufWriter::new(
        File::create(&w.part_path).map_err(|e| format!("create {}: {e}", w.part_path.display()))?,
    );

    let mut reader = BufReader::new(stdout);
    let mut line = Vec::new();
    for (idx, sha) in w.shas.iter().enumerate() {
        stdin.write_all(sha.as_bytes()).map_err(|e| format!("write sha: {e}"))?;
        stdin.write_all(b"\n").map_err(|e| format!("write nl: {e}"))?;
        stdin.flush().map_err(|e| format!("flush stdin: {e}"))?;

        line.clear();
        reader
            .read_until(b'\n', &mut line)
            .map_err(|e| format!("read hdr: {e}"))?;
        if line.is_empty() {
            return Err("git cat-file: unexpected EOF".into());
        }
        let hdr = line[..line.len().saturating_sub(1)].to_vec();
        // "<sha> <type> <size>"
        let mut it = hdr.split(|&b| b == b' ');
        let _sha = it.next();
        let typ = it.next().ok_or("bad hdr")?;
        let size: usize = String::from_utf8_lossy(it.next().ok_or("bad hdr")?)
            .trim()
            .parse()
            .map_err(|e| format!("bad size: {e}"))?;
        let mut content = vec![0u8; size];
        reader
            .read_exact(&mut content)
            .map_err(|e| format!("read content: {e}"))?;
        reader
            .read_exact(&mut [0u8; 1]) // trailing newline
            .map_err(|e| format!("read nl: {e}"))?;

        let mut raw = Vec::with_capacity(typ.len() + 1 + size.to_string().len() + 1 + size);
        raw.extend_from_slice(typ);
        raw.push(b' ');
        raw.extend_from_slice(size.to_string().as_bytes());
        raw.push(0);
        raw.extend_from_slice(&content);

        let loose = zlib_level1(&raw);
        let name = format!("{}/{}", &w.shas[idx][0..2], &w.shas[idx][2..]);
        let entry = tar_entry(&name, &loose);
        out.write_all(&entry).map_err(|e| format!("write entry: {e}"))?;
        w.done.fetch_add(1, Ordering::Relaxed);
    }
    let _ = out.flush();
    let _ = stdin.flush();
    drop(stdin); // close git's stdin -> git exits after the last response
    let _ = child.wait();
    Ok(())
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 4 {
        eprintln!(
            "usage: bb-obj-tar <gitdir> --out <objects.atl.tar> [--chunks N] [--jobs M]"
        );
        std::process::exit(2);
    }
    let gitdir = PathBuf::from(&args[1]);
    let mut out_path = PathBuf::new();
    let mut chunks = num_cpus();
    let mut i = 2;
    while i < args.len() {
        match args[i].as_str() {
            "--out" => {
                out_path = PathBuf::from(&args[i + 1]);
                i += 2;
            }
            "--chunks" => {
                chunks = args[i + 1].parse().unwrap_or(num_cpus());
                i += 2;
            }
            _ => {
                eprintln!("unknown arg: {}", args[i]);
                std::process::exit(2);
            }
        }
    }
    if out_path.as_os_str().is_empty() {
        eprintln!("--out required");
        std::process::exit(2);
    }

    // Locate the single pack produced by `git repack -adf`.
    let packdir = gitdir.join("objects").join("pack");
    let mut packs: Vec<PathBuf> = Vec::new();
    if let Ok(rd) = std::fs::read_dir(&packdir) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().map(|x| x == "idx").unwrap_or(false) {
                packs.push(p);
            }
        }
    }
    if packs.is_empty() {
        eprintln!("no pack idx in {}", packdir.display());
        std::process::exit(1);
    }
    let idx = &packs[0];
    let shas = match idx_shas(idx) {
        Ok(s) => s,
        Err(e) => {
            eprintln!("{e}");
            std::process::exit(1);
        }
    };
    let total = shas.len();
    eprintln!("[objtar] {total} objects from {}", idx.display());

    let nchunks = chunks.min(total).max(1);

    // Split shas into contiguous chunks.
    let mut chunks_of: Vec<Vec<String>> = Vec::with_capacity(nchunks);
    for c in 0..nchunks {
        let start = c * total / nchunks;
        let end = (c + 1) * total / nchunks;
        chunks_of.push(shas[start..end].to_vec());
    }

    let done = Arc::new(AtomicU64::new(0));
    let start = Instant::now();
    let mut handles = Vec::new();
    for (c, chunk) in chunks_of.into_iter().enumerate() {
        let part = out_path.with_extension(format!(
            "part.{:03}",
            c
        ));
        let done = Arc::clone(&done);
        let gdir = gitdir.clone();
        handles.push(thread::spawn(move || {
            run_worker(Worker {
                shas: chunk,
                gitdir: gdir,
                part_path: part,
                done,
            })
        }));
    }

    // Progress reporter thread.
    let done_p = Arc::clone(&done);
    let reporter = thread::spawn(move || {
        let mut last = 0u64;
        loop {
            thread::sleep(Duration::from_millis(2000));
            let d = done_p.load(Ordering::Relaxed);
            let el = start.elapsed().as_secs_f64().max(0.001);
            let rate = d as f64 / el;
            let pct = d as f64 / total as f64 * 100.0;
            let eta = if rate > 0.0 {
                ((total as f64 - d as f64) / rate) as u64
            } else {
                0
            };
            if d != last {
                eprintln!(
                    "[objtar] unpacked {d}/{total} ({pct:4.1}%, {rate:.0}/s, ETA {eta}s)"
                );
                last = d;
            }
            if d >= total as u64 {
                break;
            }
        }
    });

    let mut first_err: Option<String> = None;
    for h in handles {
        if let Err(e) = h.join().unwrap() {
            if first_err.is_none() {
                first_err = Some(e);
            }
        }
    }
    let _ = reporter.join();
    if let Some(e) = first_err {
        eprintln!("[objtar] worker error: {e}");
        std::process::exit(1);
    }

    // Join parts: stream-concatenate + 1024 zero bytes (end-of-archive marker).
    // Streamed part-by-part so memory stays O(part), never O(total archive).
    let mut out = BufWriter::new(File::create(&out_path).map_err(|e| eprintln!("create out: {e}")).unwrap());
    for c in 0..nchunks {
        let part = out_path.with_extension(format!("part.{:03}", c));
        let mut f = BufReader::new(File::open(&part).unwrap());
        std::io::copy(&mut f, &mut out).unwrap();
        drop(f);
        std::fs::remove_file(&part).unwrap();
    }
    out.write_all(&[0u8; 1024]).unwrap();
    drop(out);
    eprintln!(
        "[objtar] wrote {} ({} entries, {:.1}s)",
        out_path.display(),
        total,
        start.elapsed().as_secs_f64()
    );
}

fn num_cpus() -> usize {
    std::thread::available_parallelism().map(|n| n.get()).unwrap_or(4).max(1)
}
