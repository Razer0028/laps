---
name: cpp-cmake
description: C and C++ development - modern CMake projects, compilers (MSVC, MinGW, GCC, Clang), dependencies with vcpkg or Conan, build types, warnings, sanitizers and debugging (gdb/lldb/Visual Studio), common linker and runtime errors, and modern C++ practices. Use for any C/C++ code or build problem.
keywords: c++, cmake, cmakelists.txt, msvc, mingw, vcpkg, conan, clang++, g++
---

# C/C++ with CMake

## Configure and build (out of source)
```
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release     # single-config generators
cmake --build build -j
ctest --test-dir build --output-on-failure
```
Visual Studio generator is multi-config: `cmake -S . -B build -G "Visual Studio 17 2022" -A x64` then `cmake --build build --config Release`. On Windows run from a "Developer" environment (or `vcvars64.bat`) when using Ninja with MSVC.

## Modern CMake rules
```cmake
cmake_minimum_required(VERSION 3.20)
project(app LANGUAGES CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
add_library(core src/core.cpp)
target_include_directories(core PUBLIC include)
target_compile_options(core PRIVATE $<$<CXX_COMPILER_ID:MSVC>:/W4 /permissive-> $<$<NOT:$<CXX_COMPILER_ID:MSVC>>:-Wall -Wextra -Wpedantic>)
add_executable(app src/main.cpp)
target_link_libraries(app PRIVATE core)
```
Targets and `target_*` commands only; no global `include_directories`/`link_libraries`; no `file(GLOB)` for sources in serious projects.

## Dependencies
- vcpkg manifest mode: `vcpkg.json` with dependencies, configure with `-DCMAKE_TOOLCHAIN_FILE=<vcpkg>/scripts/buildsystems/vcpkg.cmake`, then `find_package(fmt CONFIG REQUIRED)` + `target_link_libraries(app PRIVATE fmt::fmt)`.
- Conan 2: `conan install . --output-folder=build --build=missing` then use the generated toolchain.
- Small header-only libs or pinned sources: `FetchContent`.

## Debugging
- Debug builds with symbols (`-DCMAKE_BUILD_TYPE=Debug` or RelWithDebInfo). Sanitizers (GCC/Clang): `-fsanitize=address,undefined -fno-omit-frame-pointer`; MSVC: `/fsanitize=address`.
- Crash: run under `gdb -batch -ex run -ex bt --args ./app ...` or `lldb`; on Windows read the exception code (0xC0000005 = access violation, 0xC0000135 = DLL not found).
- Linker errors: "undefined reference/unresolved external" = missing library in `target_link_libraries`, missing definition, C vs C++ linkage (`extern "C"`), or mismatched runtime/architecture. "multiple definition" = function defined in a header without `inline`.
- Windows runtime: MSVC `/MD` vs `/MT` must match across libraries; ship needed DLLs next to the exe (check with `dumpbin /dependents app.exe`).

## Practices
RAII and smart pointers (`std::unique_ptr` by default), no owning raw pointers, `std::span`/`std::string_view` for non-owning views, `const` correctness, value semantics, avoid macros, prefer `std::array`/`std::vector`. Enable warnings as errors in CI.
