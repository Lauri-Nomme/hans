---
id: f2e99fad-5cff-439b-86df-553b595a3771
created: '2026-08-21T11:50:43.134Z'
modified: '2026-08-21T11:50:43.134Z'
memory_type: learning
tags:
  - hans
  - macos
  - rust
  - osxcross
  - cross-compile
  - bb-obj-tar
  - bb-merge-base
---
hans: added macOS support to the Rust acceleration helpers (bb-obj-tar, bb-merge-base). Now committed universal Mach-O binaries bb-obj-tar-macos + bb-merge-base-macos (arm64+x86_64 slices) in tools/*/dist/, built cross from this Linux box.

HOW the mac cross-build works (reproduces; see tools/BUILD.md):
1. rustup target add aarch64-apple-darwin x86_64-apple-darwin
2. Install osxcross (git clone github.com/tpoechtrager/osxcross; UNATTENDED=1 BUILD_FLAVOR=stable ./build.sh). Needs an SDK tarball in tarballs/: sourced MacOSX15.5.sdk.tar.xz (78MB, universal archs) from github.com/joseluisq/macosx-sdks/releases (a public Apple-SDK repack commonly used for CI cross-compiles) — Apple itself license-gates SDK downloads. libxml2-dev/libssl-dev etc needed as build deps.
3. Per-tool .cargo/config.toml sets [target.*apple-darwin] linker = osxcross <arch>-apple-darwin24.5-clang, plus rustflags link-arg=-fuse-ld=<osxcross>/<arch>-apple-darwin24.5-ld. (gitignored — machine-specific paths; the committed recipe is BUILD.md.)
4. CC_<triple>/AR_<triple> env must point at osxcross clang/ar for the C deps (libz-sys, libgit2-sys). SDKROOT env must be exported (= osxcross target/SDK/MacOSX15.5.sdk) or final link fails on -liconv.
5. cargo build --release --target aarch64-apple-darwin + x86_64-apple-darwin; then osxcross <arch>-lipo -create both into universal dist/<tool>-macos.

OBSTACLES hit & fixed:
- zigbuild route fails: git2/libgit2 on macOS links -framework Security -framework CoreFoundation; zig's darwin target has NO framework stubs -> 'unable to find framework'. osxcross SDK provides them.
- osxcross default clang wrapper invokes HOST GNU ld (unrecognised emulation llvm). Fix: explicit -fuse-ld=<osxcross>/<arch>-ld in rustflags.
- sysroot: -C link-arg=-isysroot=/path was misparsed ('no such sysroot directory: =/path'); use SDKROOT env instead and drop the isysroot link-arg.
- bb-merge-base mac binary links Security.framework + CoreFoundation.framework (vendored libgit2 macOS https/keychain backend), plus /usr/lib/libSystem + libiconv.2.dylib — all system libs present on any Mac, so universal binaries are dependency-clean. bb-obj-tar links only libSystem.

Python side: bb_archiver/cli.py _autodetect_{obj_tar,merge_base}_bin now maps sys.platform 'darwin' -> '<tool>-macos' (was None -> silent git fallback on Mac). corpus/verify_merge_base.py cands list gained dist/bb-merge-base-macos. Linux/Windows paths unchanged.

Verification on Linux: file = Mach-O universal binary (arm64 + x86_64); both slices contain usage strings; linux binary still passes corpus/verify_merge_base.py.

Caveats for the target Mac admins: binaries are UNSIGNED; on a real Mac Gatekeeper quarantines downloaded binaries — first run needs 'xattr -cr <binary>' (or right-click open). Deployment target: libgit2 pushes min to 11.0 (macOS 11 Big Sur+).

Usage: python3 -c "import bb_archiver.cli as c; c.main(['assemble', ...])" autodetects the mac binary on darwin automatically; no git fallback needed on Mac.
