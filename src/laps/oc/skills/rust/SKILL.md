---
name: rust
description: Rust development - cargo workflows, workspace layout, error handling (thiserror/anyhow), ownership and borrowing fixes, traits and generics, async with tokio, CLI tools (clap), serde, testing, clippy/rustfmt, features, cross-compilation (including Windows targets from Linux), and release builds. Use for any Rust code.
keywords: rust, cargo, cargo.toml, crate, rustc
---

# Rust

## Workflow
- `cargo check` (fast feedback) -> `cargo clippy --all-targets -- -D warnings` -> `cargo test` -> `cargo build --release`. Format with `cargo fmt`.
- Read compiler errors fully: the help lines usually contain the fix. Fix the first error first.
- Toolchain pinned in `rust-toolchain.toml` if the project has one. Add deps with `cargo add serde --features derive`.

## Errors
- Libraries: an error enum with `thiserror`; applications: `anyhow::Result` with `.context("reading config")?`.
- No `unwrap()`/`expect()` on fallible runtime input in production paths; fine in tests and for true invariants (with a message).

## Ownership quick fixes
- "borrowed value does not live long enough": return owned data (`String`, `Vec`) instead of references, or restructure so the owner outlives the borrow.
- "cannot borrow as mutable because it is also borrowed as immutable": end the immutable borrow first (copy the needed value out, use indices, or split the struct borrow).
- Shared mutable state across threads: `Arc<Mutex<T>>` (or `RwLock`, channels); across async tasks use `tokio::sync::Mutex` only if held across `.await`.
- Prefer `&str`/`&[T]` parameters, return owned types. Clone deliberately, not reflexively; `Cow` when sometimes-owned.

## Common crates
serde/serde_json (data), clap derive (CLI), tokio (async runtime), reqwest (HTTP client), axum (HTTP server), tracing + tracing-subscriber (logging), rayon (data parallelism), anyhow/thiserror (errors), sqlx (async SQL).

## Async
`#[tokio::main] async fn main()`; never block inside async (use `tokio::task::spawn_blocking` for CPU or blocking I/O); timeouts with `tokio::time::timeout`; cancel by dropping futures / `select!`.

## Testing
Unit tests in `#[cfg(test)] mod tests` next to code; integration tests in `tests/`; `cargo test -- --nocapture` to see output; doc tests run code in `///` examples.

## Cross-compilation and release
- Windows exe from Linux: `rustup target add x86_64-pc-windows-gnu` + mingw-w64 linker (`apt install mingw-w64`), then `cargo build --release --target x86_64-pc-windows-gnu`. MSVC target needs Windows or cargo-xwin.
- Linux static binaries: `x86_64-unknown-linux-musl` target.
- Smaller/faster release: in Cargo.toml `[profile.release] lto = "thin"`, `codegen-units = 1`, `strip = true`.
