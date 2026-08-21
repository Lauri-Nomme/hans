# Build the Rust helper binaries

Both tools are pure portable Rust; the only C they pull in is transitive
(`libz-sys` for `flate2`/`libgit2-sys`). Binaries are committed under each
tool's `dist/` so users never need Rust installed.

## Release profile (both crates, `[profile.release]`)

```
lto = "fat"
codegen-units = 1
opt-level = 3
panic = "abort"
strip = true
```

## Current dist binaries

| tool | platform | arch | file |
|------|----------|------|------|
| bb-obj-tar | linux | x86_64 | `dist/bb-obj-tar-linux-x86_64` |
| bb-obj-tar | windows | x86_64 | `dist/bb-obj-tar-windows-x86_64.exe` |
| bb-obj-tar | macos | universal (arm64+x86_64) | `dist/bb-obj-tar-macos` |
| bb-merge-base | linux | x86_64 | `dist/bb-merge-base-linux-x86_64` |
| bb-merge-base | windows | x86_64 | `dist/bb-merge-base-windows-x86_64.exe` |
| bb-merge-base | macos | universal (arm64+x86_64) | `dist/bb-merge-base-macos` |

The Python autodetector (`bb_archiver/cli.py._autodetect_*_bin`) picks by
`sys.platform`:
`win32` → `.exe`, `linux` → `-linux-x86_64`, `darwin` → `-macos`.

## Native build (any host, incl. macOS)

```sh
cd tools/<tool>
cargo build --release            # host target; output target/release/<tool>
```

## Linux x86_64 (native on this box)

```sh
cargo build --release --target x86_64-unknown-linux-gnu
cp target/x86_64-unknown-linux-gnu/release/<tool> dist/<tool>-linux-x86_64
```

## Windows x86_64 (cross from Linux, mingw-w64)

```sh
rustup target add x86_64-pc-windows-gnu
# requires x86_64-w64-mingw32 gcc (Debian: gcc-mingw-w64-x86-64)
CC_x86_64_pc_windows_gnu=x86_64-w64-mingw32-gcc \
cargo build --release --target x86_64-pc-windows-gnu
cp target/x86_64-pc-windows-gnu/release/<tool>.exe dist/<tool>-windows-x86_64.exe
```

## macOS (cross from Linux, osxcross)

Apple's SDK is license-gated but a pre-packaged open-source SDK tarball is
available (used by many CI pipelines). Steps used here (Linux → osxcross):

```sh
# 1. install osxcross + an SDK (see osxcross/README.SDK.md). Any recent SDK
#    works; the universal deployment target is raised by libgit2's min 11.0.
git clone https://github.com/tpoechtrager/osxcross.git /tmp/osxcross
#   place MacOSX<ver>.sdk.tar.xz into /tmp/osxcross/tarballs/
UNATTENDED=1 BUILD_FLAVOR=stable /tmp/osxcross/build.sh

# 2. rustup darwin targets
rustup target add aarch64-apple-darwin x86_64-apple-darwin

# 3. per-tool cargo config (linker + ld64 + sysroot) — see
#    tools/<tool>/.cargo/config.toml (committed; paths must point at the
#    osxcross install you built).
#    NB: /tmp/opencode/oxtool is the location used here; adjust to your build.

# 4. build both archs (SDKROOT must be exported for the C deps' sysroot)
export SDKROOT=/tmp/opencode/oxtool/target/SDK/MacOSX15.5.sdk
export CC_aarch64_apple_darwin=.../aarch64-apple-darwin24.5-clang
export AR_aarch64_apple_darwin=.../aarch64-apple-darwin24.5-ar
export CC_x86_64_apple_darwin=.../x86_64-apple-darwin24.5-clang
export AR_x86_64_apple_darwin=.../x86_64-apple-darwin24.5-ar
cargo build --release --target aarch64-apple-darwin
cargo build --release --target x86_64-apple-darwin

# 5. lipo into a universal binary
.../aarch64-apple-darwin24.5-lipo -create \
  target/aarch64-apple-darwin/release/<tool> \
  target/x86_64-apple-darwin/release/<tool> \
  -output dist/<tool>-macos
```

Caveats:
- `bb-merge-base` links `Security.framework` + `CoreFoundation.framework`
  (macOS keychain/https backend inside vendored libgit2); these are system
  frameworks present on every Mac, so the universal binary has no external
  `dist/` deps — verified `otool`-style: only `/usr/lib/libSystem`,
  `CoreFoundation`, `Security`, `libiconv`.
- The `ld64` linker from osxcross is used via `-fuse-ld=<...>/<arch>-ld`;
  the sysroot must be resolvable (SDKROOT env) or link fails on `-liconv`.
- macOS binaries are unsigned; on a real Mac the OS may apply Gatekeeper
  quarantine. First run may need `xattr -cr <binary>` (or right-click → Open).