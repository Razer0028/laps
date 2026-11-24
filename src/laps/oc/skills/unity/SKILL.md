---
name: unity
description: Unity engine work - project settings and version control (.meta, Force Text, LFS, YAML merge), folder/assembly layout, C# gameplay patterns, Addressables, UI Toolkit/uGUI, performance (GC, batching, profiler), and command-line batch builds with correct exit codes. Use for any Unity project task.
keywords: unity, monobehaviour, prefab, .unity, addressables, ui toolkit, unityエディタ
---

# Unity

## Project setup (do first)
- Editor settings: Asset Serialization = Force Text, Version Control = Visible Meta Files.
- Commit every `.meta` file (losing them breaks all references). Never commit `Library/ Temp/ Obj/ Build/ Builds/ Logs/ UserSettings/ *.csproj *.sln`.
- `.gitattributes`: LFS for `*.psd *.fbx *.png *.jpg *.tga *.wav *.mp3 *.ogg *.mp4 *.ttf *.otf`; `*.unity *.prefab *.asset *.mat *.anim *.controller merge=unityyamlmerge eol=lf` with UnityYAMLMerge configured as the merge tool.
- Scenes and prefabs conflict when two people edit them: split scenes (additive loading) and use prefab variants/nested prefabs.
- Find the editor version in `ProjectSettings/ProjectVersion.txt`; build with exactly that version.

## Layout
```
Assets/
  _Project/
    Scripts/Runtime/   Game.Runtime.asmdef
    Scripts/Editor/    Game.Editor.asmdef (Editor platform only)
    Scripts/Tests/     Game.Tests.asmdef
    Art/ Audio/ Prefabs/ Scenes/ Settings/ UI/
  ThirdParty/          (store assets, kept separate for clean updates)
```
Assembly definitions speed up compiles and keep editor code out of builds.

## C# patterns
- Cache component references in `Awake`; never `GetComponent`/`Find` in `Update`.
- No allocations per frame: avoid LINQ, string concatenation, boxing, `new` lists in hot paths; reuse buffers; use `NonAlloc` physics queries.
- `[SerializeField] private` fields instead of public fields. ScriptableObjects for shared config and data tables.
- Coroutines for simple sequences; async/await with `Awaitable` (Unity 2023.1+) or UniTask for complex flows; always handle cancellation when objects are destroyed (`destroyCancellationToken`).
- Physics in `FixedUpdate`, input reading in `Update`. Use the new Input System with action maps.
- Unity's `==` null check is overloaded: destroyed objects compare equal to null, but `?.` and `??` bypass it - do not use them on UnityEngine.Object.

## Assets and loading
- Avoid `Resources/` (everything inside ships and loads slowly). Use Addressables: load by label/key, release handles you load (`Addressables.Release`), build content before the player build.
- Textures: set max size and compression per platform (BC7/DXT on PC, ASTC on mobile); disable Read/Write unless needed; sprite atlases for UI.
- Audio: Vorbis + Streaming for music, Decompress On Load for short SFX, Compressed In Memory for medium clips.

## UI
- uGUI: separate canvases for static and frequently changing elements (any change rebuilds the whole canvas); disable Raycast Target on non-interactive graphics; use TextMeshPro.
- UI Toolkit (runtime): USS for styling, UXML for structure; good for menus and tools, data-heavy screens.
- Canvas Scaler: Scale With Screen Size, reference 1920x1080, match 0.5; test multiple aspect ratios.

## Performance
- Profile first (Profiler, Deep Profile briefly, Memory Profiler, Frame Debugger). Target the biggest bar.
- Batching: SRP Batcher (URP/HDRP), GPU instancing for repeated meshes, static batching for static geometry, atlas sprites.
- Object pooling (`UnityEngine.Pool.ObjectPool<T>`).
- Physics: reduce layers in the Layer Collision Matrix, use primitive colliders, raise Fixed Timestep only if acceptable.
- IL2CPP + code stripping for release; check `link.xml` when reflection-based code breaks after stripping.

## Command-line builds
```
"<Unity>/Editor/Unity.exe" -quit -batchmode -nographics -projectPath "<project>" ^
  -executeMethod BuildScript.BuildWindows -logFile -
```
- `-logFile -` sends the log to stdout; without it you see nothing.
- Batch mode can exit 0 even when the build failed. Exit explicitly:
```csharp
using UnityEditor; using UnityEditor.Build.Reporting;
public static class BuildScript {
  public static void BuildWindows() {
    var r = BuildPipeline.BuildPlayer(new BuildPlayerOptions {
      scenes = EditorBuildSettingsScene.GetActiveSceneNames(),
      locationPathName = "Build/Win/Game.exe", target = BuildTarget.StandaloneWindows64 });
    EditorApplication.Exit(r.summary.result == BuildResult.Succeeded ? 0 : 1);
  }
}
```
- Tests: `-runTests -testPlatform EditMode -testResults results.xml` (do not combine with `-quit`).
- Only one editor instance can open a project: close the editor or use a copy of the project for CLI builds.
