---
name: game-file-analysis
description: Analyze a game's files - identify the engine and data formats, then extract or convert assets (textures, sprites, audio, text, models) with standard open-source tools and scripts. Covers Unity (UnityPy, AssetRipper), Unreal (FModel, repak), Godot, Ren'Py, RPG Maker, Wolf RPG, GameMaker, Kirikiri and other visual-novel engines, Source, Bethesda, RE Engine, XNA/MonoGame, Flash, Love2D, Cocos, Electron/HTML5, PyInstaller and Java games, and audio middleware (Wwise, FMOD, CRI). Use for modding, translation, asset inspection, or "what is in this game folder".
keywords: ゲーム+抽出, .utoc, .locres, fmodel, retoc, .vrm, .pmx, .fbx, model3.json, モデル+解析, ゲーム+解析, ゲーム+アセット, ゲーム+取り出, ゲーム+mod, ゲーム+仕組み, セーブデータ, assetbundle, streamingassets, unity3d, .assets, .uasset, .pak, unreal+抽出, ren'py, rpgツクール, ツクール, kirikiri, 吉里吉里, .xp3, wwise, fmod, .acb, .awb, live2d, .moc3, il2cpp, global-metadata, assetripper, assetstudio, assetstudiomodcli, unitypy, fbx, skinnedmeshrenderer, ボーン+モデル, blender+fbx, モデル+抽出, キャラ+モデル, monobehaviour, assembly-csharp, binaryformatter, .vpk, .bsa, .ba2, .esp, .nif, .mdl+source, .xnb, monogame, .swf, flash, .love, love2d, cocos, .jsc, .wolf, ウディタ, .nsa, nscripter, siglus, .cpk, criware, pyinstaller, .asar, re engine, frostbite, webgl+unity, ゲーム+エンジン, どのエンジン
---

# Game file analysis

Scope: files the user has on their machine, for inspection, modding, translation, preservation or their own projects. Do not redistribute extracted assets. Removing DRM, license checks or online anti-cheat is out of scope; if a task needs that, say so and stop. Encrypted archives: proceed only with a key the user provides (their own project or one they are entitled to use); do not try to break encryption or dig keys out of executables.

## Workflow
1. **Identify before extracting.** Run `python scripts/identify.py "<game folder>"`. It reads magic bytes (not extensions), finds engine markers, lists the biggest data files, and suggests the tool for each format. Read its verdict and evidence.
2. **Let the tools read structure and bytes for you.** Read on a binary returns its internal structure for common formats (Unity bundles with per-block compression and contained files, ZIP, Windows executables, FMOD FSB5, WAV, PNG with chunk CRCs). To look for a signature or magic number use Grep with bytes=true (pattern as text, or hex:4d4f4333); to see bytes at an offset use Read with hex=true. Never convert text to hex or read little/big-endian values by hand — the tools compute them.
   A Unity bundle whose blocks are LZ4/LZMA-compressed shows only compressed data to a raw byte scan (short strings may survive, but headers and numbers after them are garbled). Load it with UnityPy instead of scanning the file.
   For Unity, Read on a bundle or `.assets` file also lists the objects per type, whether a type tree is present, and MonoBehaviours per script class with namespace (e.g. how many CubismMoc), resolving scripts that live in another file (globalgamemanagers.assets). `map=true` shows embedded files and compressed regions; `compare_with` diffs two files.
   **Navigate Unity objects with Read, not UnityPy scripts**: `find=<name>` (on a bundle, an `.assets` file or the whole `<Game>_Data` folder; `a|b` for several) lists GameObjects with their components, MonoBehaviours grouped by script class, MonoScripts and named assets, each with its path_id. `path_id=<n>` shows one object: a GameObject's components, parent and children; a MonoBehaviour's GameObject, script (class, namespace, assembly) and its **field values read from the game's `Managed/*.dll` even when there is no type tree** (UnityPy's `read_typetree()` raises ValueError then), with byte[] fields located by offset (e.g. a CubismMoc's moc3 bytes); `byte_offset`/`byte_length` then show the object's own bytes. MonoScript `m_Name` is often not the class name — the tools use `m_ClassName`.
   On a .NET DLL (`Managed/Assembly-CSharp.dll`), Read lists the types and the MonoBehaviour/ScriptableObject subclasses; `find=<class>` gives its fields in serialized order (types, `[SerializeField]`, which ones Unity saves) and method signatures. Decompile to C# only when you need method bodies (`ilspycmd`).
   Save data written with .NET BinaryFormatter (header `00 01 00 00 00 FF FF FF FF 01 00 00 00 00 00 00 00`), also inside gzip or as Base64 values in a JSON save, is shown by Read as an object tree. Do not load it with pythonnet/BinaryFormatter just to look (deserializing untrusted data can run code).
   When unsure how UnityPy, AssetStudio, AssetRipper or AssetsTools.NET really behave, read their source instead of guessing the API.
   Every structure Read returns (binary structure, `find`, `path_id`, model summaries) is also saved under `.blackwall/analysis/` in the workspace, one Markdown file per target, listed in `.blackwall/analysis/INDEX.md`. Check there before reading the same file again.
3. **Prefer scripts and CLIs over GUI tools.** Driving a GUI (AssetStudio, GARbro, FModel) through the Desktop tool is slow and fragile; use the library/CLI route below and fall back to a GUI only when no CLI exists.
4. **Extract into a separate output folder** (never write into the game folder). Keep the original relative paths so the user can find things.
5. **Convert only after extracting** (e.g. WAV -> MP3 with ffmpeg) and report counts: found / extracted / failed, with the first error of each kind. When every object of one type fails, stop and fix that first error (often a missing decoder package or the wrong folder) before running the batch again — do not rerun it unchanged.
6. **Get the tools from their official release pages** (AssetStudioModCLI, AssetRipper, Il2CppDumper, Cpp2IL, UABEA, BepInEx, MelonLoader, UnityExplorer, FModel, UModel, repak, retoc, UAssetGUI, UE4SS, vgmstream, wwiser, ffmpeg, GDRE_tools, unrpyc, GARbro, RPGMakerDecrypter, UTMT_CLI, QuickBMS, Blender, assimp, FBX2glTF, texconv, astcenc, ILSpy, dnSpy, 7zip) and unpack them into the tools folder shown in <env>. Python packages (UnityPy, unrpa) with pip in a venv. Never guess download URLs. For files that are not game-specific (executables, installers, PDFs, unknown binaries) see the file-analysis skill.


## Engine signatures (what identify.py looks for)
| Engine | Markers |
|---|---|
| Unity (Mono) | `<Game>_Data/`, `UnityPlayer.dll`, `globalgamemanagers`, `*.assets`, `*.resS`, `Managed/Assembly-CSharp.dll` |
| Unity (IL2CPP) | `GameAssembly.dll` + `<Game>_Data/il2cpp_data/Metadata/global-metadata.dat` |
| Unity bundles | files starting with `UnityFS` (often `.bundle`, `.ab`, or no extension; Addressables under `StreamingAssets/aa/`) |
| Unreal 4/5 | `*.pak` (magic `E1 12 6F 5A` near the end), `*.utoc` + `*.ucas` (IoStore), `<Game>/Content/Paks/` |
| Godot | `.pck` starting with `GDPC`, or a `.exe` with an embedded pck |
| Ren'Py | `renpy/`, `game/*.rpa` (`RPA-3.0`), `*.rpyc` |
| RPG Maker MV/MZ | `www/js/rpg_core.js` or `js/rmmz_core.js`, `data/System.json`; encrypted assets `.rpgmvp/.rpgmvo/.png_/.ogg_` |
| RPG Maker XP/VX/Ace | `Game.rgssad` / `.rgss2a` / `.rgss3a` |
| GameMaker | `data.win` (starts with `FORM`) |
| Kirikiri (KAG) | `*.xp3` (starts with `XP3\r\n`) |
| Electron / NW.js | `resources/app.asar`, `package.nw` |
| Unity WebGL | `Build/*.data(.br/.gz)` + `*.framework.js` (`UnityWebData1.0` header) |
| Wolf RPG Editor | `Data.wolf` / `Data/BasicData/Game.dat` |
| Source | `*.vpk` (`34 12 AA 55`), `*.bsp`, `gameinfo.txt` |
| Bethesda | `*.bsa` (`BSA\0`), `*.ba2` (`BTDX`), `*.esm/.esp` |
| RE Engine | `re_chunk_000.pak` (`KPKA`) |
| XNA / FNA / MonoGame | `Content/*.xnb` (`XNB`) |
| Flash / AIR | `*.swf` (`FWS`/`CWS`/`ZWS`) |
| LOVE | `*.love` (zip with `main.lua`), or zip appended to the exe |
| CRI | `*.cpk` (`CPK `), `*.usm` (`CRID`) |
| PyInstaller / Java | exe ending with `MEI\x0c\x0b\x0a\x0b\x0e`; `*.jar` + `lwjgl*` |
| Visual novels | `.nsa/.sar` (NScripter), `Scene.pck`/`Gameexe.dat` (Siglus), `.int` (CatSystem2), `.pfs` (Artemis) |

identify.py prints a `next:` line under each engine it detects. Read also describes VPK, BSA/BA2, SWF, XNB, RE `.pak`, CPK, UnityWebData and asar headers (file counts, version, compression).

RPG Maker MV/MZ encrypted assets: drop the first 16 bytes (the `RPGMV` header), XOR the next 16 bytes with `encryptionKey` from `data/System.json` (32 hex digits = 16 bytes), keep the rest as is. A restored image starts with `89 50 4E 47 0D 0A 1A 0A`, audio with `OggS`. Convert one file, check it with Read, then do the rest; the run is not done until an output file has been checked.

## Unity
- **UnityPy (best for scripted extraction)**: `pip install UnityPy` (audio decoding comes with it via fmod_toolkit). Use `scripts/unity_extract.py`:

      python scripts/unity_extract.py "<Game>_Data" --out extracted --list            # inventory by type
      python scripts/unity_extract.py "<Game>_Data" --out extracted --types AudioClip
      python scripts/unity_extract.py "<Game>_Data" --out extracted --types Texture2D,Sprite --name "^bg_"

  It loads the whole folder (so `.resS`/`.resource` streams resolve), exports AudioClip -> WAV, Texture2D/Sprite -> PNG, TextAsset -> raw bytes, Mesh -> OBJ (static geometry only — for characters see below), Font -> TTF/OTF, and writes `manifest.csv`. Point it at the `_Data` folder, not at single files, or streamed data will be missing.
- Audio inside Unity is usually FSB5 (Vorbis/ADPCM) in `.resource` files (`.resS` holds texture and mesh data, not audio); UnityPy converts it. If a clip fails, export the raw stream (`--raw`) and convert with `vgmstream-cli`. A real audio stream starts with `FSB5`; bytes cut from a `.resS` at an AudioClip's offset are image data, not sound.
- Games built with a compressed `data.unity3d` keep their `.resource` files next to it in `<Game>_Data/`, outside the bundle. Unpacking `data.unity3d` gives `.assets` and `.resS` but no `.resource`, so an AudioClip whose `m_Source` names `resources.resource` points to `<Game>_Data/resources.resource`, not to anything in the unpacked folder. Load the whole `<Game>_Data` folder so UnityPy finds both.
- Whole-project recovery (scenes, prefabs, scripts as a Unity project): AssetRipper (GUI/headless). Scripts: Mono games -> ILSpy (`ilspycmd`) on `Managed/*.dll`; IL2CPP -> Il2CppDumper or Cpp2IL to get type/method names.
- Addressables: bundles under `StreamingAssets/aa/<platform>/`; `catalog.json` maps keys to bundles. Some games compress bundles with LZ4/LZMA (UnityPy handles both) or apply custom encryption (then stop, see Scope).
- **Characters, outfits, anything with bones -> FBX with AssetStudioModCLI, not OBJ.** OBJ (and UnityPy's `Mesh.export()`) keeps only static geometry: no bones, skin weights, blend shapes (facial expressions), hierarchy or material links, so "which parts make up this character" is lost. Use AssetStudioModCLI (github.com/aelurum/AssetStudioMod, formerly AssetStudio; on 知識サーバー's tool shelf as `AssetStudioModCLI`):

      AssetStudioModCLI "<Game>_Data" -m info                     # Unity version, exportable counts
      AssetStudioModCLI "<Game>_Data" -m animator -o out --log-output file --log-level warning

  `-m animator` writes one FBX per GameObject with an Animator: skeleton, skinned meshes with weights, blend shapes, and textures as PNG next to it. `-m export -t mesh` gives bare meshes without bones — not enough for a character. `-m live2d` exports Live2D Cubism models. `--filter-by-name a,b` limits the export; `--fbx-animation skip|all` (default `auto` = animations found for that model). Windows: the `net472_win32_64` build runs on the .NET Framework that ships with Windows 10/11. Linux: `net9_linux64` needs the .NET 9 runtime (`dotnet-install.sh --runtime dotnet --channel 9.0`, then set `DOTNET_ROOT`). IL2CPP does not matter here: meshes and textures do not depend on scripts.
- **Do not parse Mesh vertex data by hand.** Vertex channels are often Float16, UNorm8 or SNorm16 rather than 4-byte floats, split over several streams, compressed (`m_CompressedMesh`) or stored outside in `.resS` (`m_StreamData`); a parser that assumes float32 drops or garbles a large share of the meshes. Use AssetStudioModCLI, or UnityPy's own mesh reader when you must script.
- **Check the export before converting.** Read on an exported FBX lists bones, skinned meshes, blend shapes and textures; compare with Read `find` / `-m info` (SkinnedMeshRenderer, Animator counts). Report per character (one FBX per Animator), not as thousands of loose meshes. Toon materials (e.g. MToon) and spring-bone settings do not carry over to FBX — textures are attached, the look has to be set up again; say so.
- **Blender** (split parts, clean up, re-export): use the portable build from download.blender.org (Linux `.tar.xz`, Windows `.zip`; check it against the `.sha256` file published next to it), unpacked into the Blackwall tools folder — no installer or admin rights. Run it headless: `blender -b --factory-startup -P script.py -- <args>`. Blender 4.5's FBX importer can stop with `KeyError` in `armature_setup` on some AssetStudio FBX files (a skin bound to a separate armature node); that is the importer, not a broken export — fall back to an existing armature in the importer or import the file another way instead of re-extracting.

## Unreal Engine
- **Look with Read first.** Read on a `.pak` lists every file with its size, the compression, and whether the index or the files are AES-encrypted (then a key is needed — only use a key the user provides). Read on a `.utoc` lists the IoStore container's files (the `.ucas` next to it holds the data). `find=<text>` on either lists only matching paths. Read on a `.uasset`/`.umap` gives the package header: engine version (a range when the package was saved unversioned), package path and flags, the name table, imports (referenced packages and classes) and exports (class, name, size; the asset itself first); the zen form extracted from IoStore is read too (names and exports; classes need FModel). Read on a `.locres` lists namespaces, keys and text (`find=` to search). If a `.uasset` starts with something other than `C1 83 2A 9E`, the game encrypts or alters its packages.
- Models, animations and textures of UE1–UE4 (and some UE5) games: UModel / UE Viewer (`umodel -game=ue4.27 -path=<Paks folder> -export <package>` → glTF/PSK/PNG; GUI without arguments). For UE5 and cooked IoStore content use FModel.
- Extract with `repak` (`repak unpack x.pak -o out`; `repak list` is what Read already shows) or `retoc` for IoStore (`retoc to-legacy` converts to `.pak`-style packages). Property values in cooked packages are usually "unversioned" and need a `.usmap` mapping file to be read by FModel/UAssetGUI/CUE4Parse. Make it with the UeMappings tool (it runs the game once with UE4SS and cleans up; tell the user first), then pass it to the reader (CUE4Parse: `FileUsmapTypeMappingsProvider`). Audio: Wwise `.bnk/.wem`, or `.uasset` SoundWave (FModel exports).

## Models
- Read on FBX (binary or text), glTF/GLB, VRM, OBJ/MTL, MMD PMX/PMD/VMD and Live2D `.moc3` returns what is inside: meshes with vertex and face counts, bones, skin, blend shapes/morphs with names, materials, textures with whether each file exists (or is embedded), animations with their length, units and up axis (FBX), VRM name/author/license/expressions, moc3 parts/deformers/art meshes/parameters with their IDs. Read on a `.model3.json` adds whether every referenced file (moc3, textures, physics, expressions, motions) exists and each motion's length. Use this to check an export or a mod before converting, instead of opening Blender.
- Convert or render with Blender (headless: `blender -b -P script.py`) or Assimp; for Live2D use the Cubism SDK/Viewer. A texture recorded as an absolute path from another PC is looked up next to the model by name.

## Other engines
- Godot: GDRE Tools (gdsdecomp) recovers `.pck` / embedded pck into a project, including scripts.
- Ren'Py: `pip install unrpa` -> `unrpa -mp out game/archive.rpa`; `.rpyc` -> unrpyc for readable scripts.
- GameMaker: UndertaleModTool / UndertaleModCli opens `data.win` (sprites, sounds, code).
- Kirikiri `.xp3`, many visual-novel formats: GARbro (Windows GUI; supports many archive and image formats).
- Electron: `npx @electron/asar extract resources/app.asar out` gives the JS/HTML source. NW.js `package.nw` and Love2D `.love` are zips (`7z x`); a fused Love2D exe has the zip appended (`7z x game.exe` works).
- Wolf RPG Editor: WolfDec (Windows) for `.wolf`. NScripter `.nsa/.sar`, Siglus, CatSystem2 `.int`, Artemis `.pfs`: GARbro.
- XNA / FNA / MonoGame: `xnbcli unpack Content out` (`xnbcli`); the game exe is .NET, so ILSpy reads the code.
- Flash / AIR: `ffdec` (JPEXS, needs Java) exports scripts, images, sounds and shapes; `ruffle` runs the SWF.
- Source: `pip install vpk` (`vpk -x out pak01_dir.vpk`, always open the `_dir.vpk`); models with Crowbar (Windows) to SMD/QC.
- Bethesda: `.bsa/.ba2` with BSA Browser or `bsarch`; plugins `.esp/.esm` with xEdit (Windows); meshes `.nif` with NifSkope or the Blender NIF plugin.
- RE Engine (`re_chunk_000.pak`): REtool with the game's file-name list; meshes with RE-Mesh-Editor (Blender).
- PyInstaller exe: `pyinstxtractor` (run it with the same Python minor version the game was built with), then pycdc on the `.pyc`. Java games: `7z x game.jar`, then cfr or jadx.
- CRI `.cpk`: CriFsLib.GUI (Windows) or `pip install PyCriCodecs`; the audio inside with vgmstream-cli.
- KTX/KTX2 textures: `ktx extract` (`KTX-Software`); DDS with texconv/compressonatorcli; ASTC with astcenc.
- No tool for a custom archive: look for a QuickBMS script (aluigi's list) for the game then the format specs in kaitai_struct_formats and ImHex-Patterns, before writing a parser.

## Audio middleware
- **vgmstream-cli** decodes almost every game audio format (Wwise `.wem/.bnk`, FMOD `.fsb/.bank`, CRI `.hca/.adx/.acb/.awb`, Unity FSB5 and more):

      vgmstream-cli -m file.awb                          # metadata: subsong count, codec, names
      vgmstream-cli -S 0 -o "out/?n.wav" file.awb        # all subsongs, named by internal name
      vgmstream-cli -o "out/?f.wav" file.wem

  For CRI, give it the `.acb` next to the `.awb` so names resolve. Wwise `.bnk` holds embedded `.wem`s and event data.
- Convert: `ffmpeg -i in.wav -codec:a libmp3lame -q:a 2 out.mp3` (V2 ~190 kbps); batch with a loop and keep folder structure.

## General binary inspection
- Magic bytes: first 16 bytes (`xxd -l 16 f` on Linux, `Format-Hex -Path f -Count 16` in PowerShell). Common: `PK` zip, `7z`, `Rar!`, `RIFF....WAVE`, `OggS`, `fLaC`, `ID3`, `\x89PNG`, `DDS `, `UnityFS`, `FSB5`, `BKHD` (Wwise bank), `AKPK` (Wwise pck), `@UTF` (CRI acb), `AFS2` (CRI awb), `CRID` (CRI usm video), `KB2` (Bink 2).
- `7z l file` lists many container formats; `binwalk file` finds embedded files at offsets.
- Large unexplained files: list the 30 biggest files first; data usually lives there.
- Windows paths with spaces or Japanese: always quote; in cmd run `chcp 65001` first or use PowerShell for listing.

## Report back
Engine and evidence, what was extracted (counts per type, output folder), what failed and why, and anything skipped because of encryption or scope.
