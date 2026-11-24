---
name: file-analysis
description: Find out what an unknown file is and what is inside it - identify the format, read metadata, inspect executables (Windows PE, .NET, Java/Android), unpack archives and installers without running them, look into PDFs, databases and media, and read formats nobody documented. Covers Detect It Easy, TrID, ExifTool, MediaInfo, Ghidra, radare2, ILSpy, jadx, capa, FLOSS, UPX, 7-Zip, innoextract, lessmsi, qpdf, sqlite3, ImHex and Kaitai format definitions.
keywords: ファイル+解析, ファイル+調べ, exe+調べ, dll+調べ, 何のファイル, バイナリ+解析, 形式+判定, ファイル+形式, 中身+調べ, exe+解析, dll+解析, apk+解析, jar+解析, .apk, メタデータ, exif, exiftool, pdf+構造, 逆アセンブル, 逆コンパイル, ghidra, radare2, ilspy, dnspy, jadx, detect it easy, diec, trid, imhex, 16進, パッカー, upx, インストーラ+展開, innoextract, lessmsi, msi+展開, sqlite+開, kaitai, .ksy, 署名+確認, sigcheck
---

# File analysis

Scope: files the user has, inspected to understand them, recover their own data, debug, or check what a program is. Analyze statically first — do not run an unknown executable to see what it does. Do not remove license checks or DRM, and do not try to break encryption without a key the user provides.

## Getting the tools
Use the tools already on PATH or in the tools folder first. Otherwise install them from each project's official release page or the system package manager (winget/Scoop on Windows, apt/dnf on Linux, Homebrew on macOS). Never guess download URLs.

## Workflow
1. **Identify by content, not extension.** Read on the file first: it returns the structure of common formats (ZIP, PE, Unity bundles, FSB5, WAV, PNG, models and more) and `map=true` shows embedded files. Then `diec <file>` (Detect It Easy: compiler, packer, installer, game engine) and `trid <file>` (TrID with `triddefs.trd` next to it) for formats Read does not know.
2. **Metadata**: `exiftool -a -G1 <file>` (images, video, audio, PDF, Office, executables: version info, timestamps, author, GPS); `mediainfo <file>` or `ffprobe` for containers and codecs.
3. **Choose the tool by what Detect It Easy says**, then report what was found, with counts and paths.

## Executables
- Windows PE: Read gives headers, sections, imports, resources. `sigcheck -a <file>` (Sysinternals) for the signature and version; `strings` or `floss <file>` for text, including obfuscated and stack-built strings; `capa <file>` for what the program does (network, crypto, persistence) by rules. Packed with UPX: `upx -d -o unpacked.exe <file>`. Other packers: say so; do not try to defeat them.
- Disassemble / decompile native code: Ghidra headless (`support/analyzeHeadless <projdir> <name> -import <file> -postScript …`, needs Java 21) or radare2 (`r2 -q -c "aaa; afl; pdf @ main" <file>`). Report functions and evidence, not guesses.
- .NET (Detect It Easy says .NET / `mscoree.dll` import): ILSpy (`ILSpy` GUI, or `ilspycmd -p -o out <file>` if installed) or dnSpy. Unity's `Managed/Assembly-CSharp.dll` is .NET. IL2CPP games: see the game-file-analysis skill.
- Java / Android: `jadx -d out <file.apk|.dex|.jar>` for Java source; `java -jar apktool_*.jar d <file.apk>` for resources and smali; `java -jar cfr-*.jar <file.jar>` for single classes.
- WebAssembly: `wasm2wat`, `wasm-objdump -x` (wabt).

## Archives, installers, compressed streams
- `7z l` / `7z x` (7-Zip; on Windows the full 7z.exe also reads RAR) for zip, 7z, rar, cab, iso, many self-extractors.
- Inno Setup installers: `innoextract -e -d out setup.exe`. MSI: `lessmsi x file.msi out\`. Do not run installers to get their files.
- Raw streams: zstd, lz4, xz, brotli have their own CLIs. Look at the first bytes (Read hex=true) to know which.

## Documents and data
- PDF: `qpdf --json file.pdf` for the object structure, `qpdf --qdf --object-streams=disable in.pdf out.pdf` to expand streams; `--decrypt` only with the password the user gives. Office files: they are ZIPs (docx/xlsx/pptx — Read lists the parts); old .doc/.xls with `pip install oletools` (`olevba`, `oleid`) for macros.
- SQLite (save files, app data): `sqlite3 file.db ".tables" ".schema"`, then SELECT; DB4S for a GUI. Never write to the original — copy it first.
- Disk images: The Sleuth Kit (`mmls`, `fls -r`, `icat`) reads file systems without mounting.
- Similar files: `ssdeep -r dir` (fuzzy hashes).

## Formats nobody documented
- Read with hex=true at offsets, Grep with bytes=true for signatures; let the tools compute values instead of decoding by hand.
- Look for an existing definition before writing a parser: the kaitai_struct_formats repository has (hundreds of `.ksy` definitions — executables, archives, images, audio, filesystems, game formats) and ImHex-Patterns (`.hexpat`). A `.ksy` can be compiled to Python with kaitai-struct-compiler (`-t python`) and run on the file.
- ImHex (GUI) loads `.hexpat` patterns to color the structure for the user.

## Report back
What the file is (with the evidence: magic bytes, tool verdicts), what is inside (counts, names, sizes, paths of what was extracted), what could not be read and why.
