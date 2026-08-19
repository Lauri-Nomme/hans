//! Batch merge-base for many (from,to) commit pairs using libgit2.
//!
//! Opens the repo once (so the pack/commit cache is reused across queries),
//! reads `"<from> <to>"` pairs from stdin, and writes the best merge base
//! hex sha per line (or an empty line if none). The result matches
//! `git merge-base <from> <to>`.
use std::io::{BufRead, Write};
use std::path::PathBuf;
use std::process::exit;

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: bb-merge-base <gitdir> [--nargs]");
        exit(2);
    }
    let gitdir = PathBuf::from(&args[1]);
    let mut bare = false;
    for a in &args[2..] {
        match a.as_str() {
            "--bare" => bare = true,
            other => {
                eprintln!("unknown arg: {other}");
                exit(2);
            }
        }
    }
    let repo = match if bare {
        git2::Repository::open_bare(&gitdir)
    } else {
        git2::Repository::open(&gitdir)
    } {
        Ok(r) => r,
        Err(e) => {
            eprintln!("open repo {}: {e}", gitdir.display());
            exit(1);
        }
    };

    let stdin = std::io::stdin();
    let stdout = std::io::stdout();
    let mut out = std::io::BufWriter::new(stdout.lock());
    let mut n = 0u64;
    for line in stdin.lock().lines() {
        let line = match line {
            Ok(l) => l,
            Err(_) => break,
        };
        let line = line.trim();
        if line.is_empty() {
            continue;
        }
        let mut it = line.split_whitespace();
        let from = it.next();
        let to = it.next();
        let (Some(from), Some(to)) = (from, to) else {
            eprintln!("bad pair: {line}");
            continue;
        };
        let base = merge_base(&repo, from, to);
        let _ = writeln!(out, "{base}");
        n += 1;
        if n % 1000 == 0 {
            eprintln!("[bb-merge-base] {n} pairs done");
        }
    }
    let _ = out.flush();
}

fn merge_base(repo: &git2::Repository, from: &str, to: &str) -> String {
    let one = match git2::Oid::from_str(from) {
        Ok(o) => o,
        Err(_) => return String::new(),
    };
    let two = match git2::Oid::from_str(to) {
        Ok(o) => o,
        Err(_) => return String::new(),
    };
    match repo.merge_base(one, two) {
        Ok(oid) => oid.to_string(),
        Err(_) => String::new(),
    }
}