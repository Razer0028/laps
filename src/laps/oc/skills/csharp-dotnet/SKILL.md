---
name: csharp-dotnet
description: C# and .NET - dotnet CLI, project and solution structure, NuGet, nullable reference types, async/await, dependency injection and configuration, ASP.NET Core minimal APIs, desktop UI (WPF, WinForms, MAUI, Avalonia), testing (xUnit), publishing self-contained/single-file apps, and how Unity's C# differs. Use for any C#/.NET code.
keywords: c#, csharp, .net, dotnet, nuget, .csproj, .sln, asp.net, wpf, winforms, blazor
---

# C# and .NET

## CLI
```
dotnet --list-sdks
dotnet new console -n App        # webapi, classlib, xunit, wpf, winforms, worker
dotnet new sln -n App && dotnet sln add App/App.csproj
dotnet add App package Serilog
dotnet build -c Release && dotnet test
dotnet run --project App -- arg1
```
`global.json` pins the SDK; target the current LTS (`net8.0`) unless the project says otherwise.

## Language defaults
- `<Nullable>enable</Nullable>`, `<ImplicitUsings>enable</ImplicitUsings>`, `<TreatWarningsAsErrors>true</TreatWarningsAsErrors>` for new projects.
- Records for immutable data, `required` members, pattern matching (`switch` expressions), file-scoped namespaces.
- async all the way: return `Task`/`ValueTask`, accept `CancellationToken`, never `.Result`/`.Wait()` on UI or request threads (deadlocks). `ConfigureAwait(false)` in libraries.
- `IDisposable`/`await using` for resources; `HttpClient` via `IHttpClientFactory` (do not new it per request).

## ASP.NET Core
- Minimal API: `var app = WebApplication.CreateBuilder(args).Build(); app.MapGet("/healthz", () => Results.Ok()); app.Run();`
- Configuration from `appsettings.json` + environment variables (`Section__Key`) + user-secrets in development; bind to options classes.
- DI lifetimes: Singleton (stateless/thread-safe), Scoped (per request, e.g. DbContext), Transient.
- EF Core: migrations with `dotnet ef migrations add X` / `dotnet ef database update` (install tool `dotnet tool install -g dotnet-ef`).

## Desktop
- WPF/WinForms are Windows-only; MAUI targets Windows/macOS/mobile; Avalonia is cross-platform including Linux. Use MVVM (CommunityToolkit.Mvvm) for WPF/MAUI/Avalonia.
- Never block the UI thread; marshal back with `Dispatcher`/`SynchronizationContext` (await does this automatically in UI code).
- High DPI: `<ApplicationHighDpiMode>PerMonitorV2</ApplicationHighDpiMode>` (WinForms) and test at 150%.

## Publish
`dotnet publish -c Release -r win-x64 --self-contained true -p:PublishSingleFile=true -p:IncludeNativeLibrariesForSelfExtract=true` (add `-p:PublishTrimmed=true` only if reflection is not used). Linux: `-r linux-x64`.

## Testing
xUnit + FluentAssertions (or Shouldly); `WebApplicationFactory<Program>` for API integration tests; `dotnet test --logger "console;verbosity=detailed"`.

## Unity's C#
Unity uses its own runtime (Mono or IL2CPP), C# 9-level features, its own lifecycle (Awake/Start/Update), and serialization rules. Do not use .NET-only APIs or NuGet packages without checking Unity compatibility (see the unity skill).

## Windows encodings
Console output: `Console.OutputEncoding = Encoding.UTF8;`. Reading Shift-JIS files in .NET Core+: register `CodePagesEncodingProvider.Instance` then `Encoding.GetEncoding(932)`.
