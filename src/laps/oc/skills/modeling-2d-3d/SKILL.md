---
name: modeling-2d-3d
description: Make and fix 2D and 3D art assets for games and apps - 3D modeling, retopology, UVs, rigging and weights, blend shapes, PBR materials and baking, LODs, export to FBX/glTF/VRM for Unity, Unreal, Godot and the web, VRChat avatars, MMD; 2D sprites, pixel art, sprite sheets and atlases, 9-slice UI, Live2D-style cut-out rigs, Spine/DragonBones. Mostly through headless Blender scripts and CLIs, checked by rendering and by reading the files.
keywords: モデリング, 3dモデル, 3d+モデル, blender, リトポ, uv展開, uv+展開, リギング, ウェイト, ボーン+設定, シェイプキー, ブレンドシェイプ, テクスチャ+ベイク, ベイク, pbr, lod, ポリゴン+削減, ポリゴン数, gltf, .glb, vrm, vrchat, アバター, mmd+モデル, 2d+素材, スプライト, スプライトシート, ドット絵, ピクセルアート, pixel art, アトラス, 9スライス, live2d+作, spine, dragonbones, 立ち絵, 差分+画像, modeling, retopology, rigging, sprite sheet
---

# 2D and 3D modeling

You work on assets through scripts and command-line tools, and you check them with numbers and with your eyes (renders, screenshots). A file that exports without errors can still be wrong; look at it.

## Ground rules
1. **Read the file first.** Read on FBX, glTF/GLB, VRM, OBJ, PMX and `.moc3` gives meshes, vertex/triangle counts, islands, bones, weights, blend shapes, materials, textures (and whether they exist), units and up axis, and an estimated VRChat rank. Decide what to change from that, not from guesses.
2. **Work on a copy.** Keep the original; write to `out/` with a version suffix.
3. **One change, then check.** Run the script, Read the result again, render it, compare per part (mesh/material/bone), not just totals.
4. **Render and look.** Blender headless renders front/side/back views to PNG; open them with Read. For 2D, Read the PNG or a contact sheet. If you cannot see images, report the numbers and say the look is unchecked.
5. **One heavy job at a time.** Blender, Unity and big batches together have crashed the machine; run in the background and wait.

## Tools
| Job | Tool |
|---|---|
| Any 3D edit, bake, render | `blender` (4.5 LTS) / `blender5`: `blender -b --factory-startup -P script.py -- args` |
| Format conversion | `assimp export in.fbx out.glb`, `FBX2glTF -i in.fbx -o out` (binary glTF with `-b`) |
| glTF size, LODs, compression | `gltfpack -i in.glb -o out.glb -si 0.5` (simplify), `-cc` (meshopt), `-tc` (KTX2) |
| Textures | `texconv`/`compressonatorcli` (DDS/BC), `astcenc` (ASTC), `ktx` (KTX2) |
| 2D images | Python Pillow (pip, venv); `ImageMagick` (`magick`); `LibreSprite` (pixel art GUI, Windows) |
| Look at results | Read on PNG; Serve + a three.js/model-viewer page for glTF in a browser |

## Blender scripting (bpy) essentials
- Start empty: `--factory-startup`, then `bpy.ops.wm.read_factory_settings(use_empty=True)`; import with `bpy.ops.import_scene.fbx/gltf`, `bpy.ops.wm.obj_import`.
- Scale/axes: Unity wants 1 unit = 1 m, Y up (FBX: `apply_unit_scale=True, axis_forward='-Z', axis_up='Y'`, "Apply Transform" on); Unreal is cm and Z up (export scale 1.0 with Blender scene unit 0.01 or let the FBX exporter scale); glTF is always meters, Y up — the exporter converts. Apply rotation and scale (`bpy.ops.object.transform_apply`) before rigging or export.
- Clean geometry: merge by distance (`bpy.ops.mesh.remove_doubles(threshold=0.0001)`), recalc normals outside, delete loose, triangulate only at export. Check after with Read (islands, boundary edges).
- Decimate for LODs: `Decimate` modifier (`ratio`), or gltfpack for glTF. Keep UV seams and shape keys in mind (Decimate drops shape keys; use gltfpack or do LODs before adding them).
- UVs: `bpy.ops.uv.smart_project(angle_limit=66°, island_margin=0.02)` for props; seams + `unwrap` for characters. Pack with margin ≥ 2 px at the target texture size.
- Baking (Cycles): high-poly → low-poly normal/AO with `bake(type='NORMAL', use_selected_to_active=True, cage_extrusion=...)`; the target image node must be active on the low-poly material. Normal maps: OpenGL (Y+) for Blender/Unity/Godot, DirectX (Y−) for Unreal — flip G when moving between them.
- Rigging: armature + `Armature deform with automatic weights`, then fix by script (normalize all, limit to 4 influences for mobile/VRChat, clean weights < 0.01). Read reports whether weight centres sit on their bones.
- Shape keys: keep names the target expects (VRM/ARKit `jawOpen`, `eyeBlinkLeft`, VRChat visemes `vrc.v_aa`…). Mirror with `_L/_R` or `.L/.R` naming consistently.
- Materials: Principled BSDF → glTF metallic-roughness (pack as ORM: R=AO, G=roughness, B=metallic). Toon/MToon and other custom shaders do not survive FBX; set them up again in the engine.

## Targets
- **Unity**: FBX or glTF (with a glTF importer). Humanoid needs a T-pose-ish rest and standard bone chain (hips→spine→chest→neck→head, shoulders, upper/lower arm, hand; legs). Read checks bone count and hierarchy.
- **Unreal**: FBX, Z up, cm; skeletal meshes need one root bone; LODs can ship in one FBX (`LOD_Group`).
- **Godot**: glTF/GLB preferred (`.blend` import also works with Blender on PATH).
- **Web/three.js**: GLB + gltfpack (`-cc`, `-tc`); keep draw calls low (merge materials, atlas textures).
- **VRChat/VRM**: watch triangle count, material slots, bones, skinned meshes (Read estimates the rank); VRM needs humanoid bone mapping and metadata (name, author, license) — VRM Add-on for Blender writes it.
- **MMD (PMX)**: Japanese bone names matter (センター, 上半身…); convert with mmd_tools in Blender.

## 2D
- **Sprite sheets**: same cell size for every frame, a transparent margin of 1–2 px, frames in reading order; record cell size, frame count and fps next to the sheet (JSON). Pillow: paste frames into `Image.new("RGBA", (cols*w, rows*h))`. Split a sheet back with `crop`.
- **Pixel art**: scale by whole numbers only with nearest neighbour (`Image.NEAREST`, `magick -filter point`); keep a fixed palette (`quantize(colors=N, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)`); no anti-aliasing on outlines. In engines: point filtering, no mipmaps, no compression, pixels-per-unit fixed.
- **Atlases**: pack with padding and extrude edges 1–2 px against bleeding; power-of-two sizes for older GPUs / compression; keep a JSON map name→rect (TexturePacker-style) for the engine.
- **UI**: 9-slice borders (record the four insets), export at 1x/2x (and 3x for phones), keep text out of images when it has to be translated.
- **Cut-out rigs (Live2D, Spine, DragonBones)**: split the character into layers (front hair, back hair, eyes, eyelids, mouth shapes, arms) with overlap under each cut so motion never shows holes; name layers in English, consistently left/right. Live2D models are built in the Cubism Editor (GUI only); you can prepare the layered PSD (Pillow + `psd-tools` to read/write layers) and check finished `.model3.json`/`.moc3` with Read.
- **Expression/outfit variants (立ち絵の差分)**: one base image plus overlay layers at the same canvas size and origin; compose with Pillow `alpha_composite` and check alignment by diffing against the base.
- **Upscaling/cleanup**: keep the original resolution file; upscale copies only.

## Report back
What changed (per mesh/material/bone or per sprite), the numbers before and after (triangles, bones, materials, texture sizes, file size), where the outputs and renders are, and what was not checked by eye.
