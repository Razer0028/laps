---
name: go
description: Go development - modules, project layout, error handling and wrapping, interfaces, goroutines/channels/context, HTTP servers and clients, JSON, testing (table tests, race detector), linting (go vet, staticcheck, golangci-lint), cross-compilation, and building small static binaries. Use for any Go code.
keywords: golang, go言語, go.mod, goroutine, ゴルーチン
---

# Go

## Workflow
`go mod tidy` -> `go vet ./...` -> `go test ./... -race` -> `go build ./...`. Format with `gofmt -w .` (or `goimports`). Lint with `staticcheck ./...` or `golangci-lint run` if configured.

## Layout
```
go.mod
cmd/app/main.go        # binaries
internal/...           # private packages (not importable outside the module)
pkg/...                # only if meant for external use
```
Small projects can stay flat; do not create layers before you need them.

## Code
- Errors are values: return them, wrap with context `fmt.Errorf("load config %s: %w", path, err)`, check with `errors.Is/As`. No panics for expected failures.
- Accept interfaces, return structs; define interfaces where they are used, keep them small.
- `context.Context` as the first parameter for anything that does I/O or can be cancelled; respect `ctx.Done()`.
- Goroutines must have an owner and an exit path: use `errgroup.Group` (`golang.org/x/sync/errgroup`) for fan-out with error handling; close channels from the sender side only.
- Protect shared state with `sync.Mutex` or confine it to one goroutine; run tests with `-race`.
- HTTP server: set timeouts (`ReadHeaderTimeout`, `ReadTimeout`, `WriteTimeout`, `IdleTimeout`); graceful shutdown with `srv.Shutdown(ctx)` on SIGINT/SIGTERM. Go 1.22+ `http.ServeMux` supports method and path patterns (`mux.HandleFunc("GET /items/{id}", h)`).
- HTTP client: reuse one `http.Client` with a `Timeout`; always `defer resp.Body.Close()` and check status codes.
- Logging: `log/slog` (structured).

## Testing
Table-driven tests with `t.Run(name, ...)`; `t.TempDir()`; `httptest.NewServer` for HTTP; benchmarks `func BenchmarkX(b *testing.B)` with `go test -bench . -benchmem`.

## Build and cross-compile
- `CGO_ENABLED=0 GOOS=windows GOARCH=amd64 go build -trimpath -ldflags "-s -w" -o app.exe ./cmd/app` (on Windows cmd: `set GOOS=windows&& ...` or PowerShell `$env:GOOS='windows'`).
- Embed static files with `//go:embed`. Version info via `-ldflags "-X main.version=1.2.3"`.
