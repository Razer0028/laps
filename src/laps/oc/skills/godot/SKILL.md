---
name: godot
description: Godot 4 development - project structure, scenes and nodes, GDScript (static typing, signals, @export, autoloads), resources, input map, UI with Control nodes and themes, performance, and headless CLI exports. Use for any Godot project.
keywords: godot, gdscript, .gd, .tscn, .tres
---

# Godot 4

## Project basics
- `project.godot` at the root; everything is under `res://`. User data (saves, settings) goes to `user://` (OS-specific app data folder).
- Version control: do not commit `.godot/` (cache); commit `*.import` files and `export_presets.cfg` (without secrets). Godot generates `.gitignore` with `.godot/`.
- Prefer composition: a scene per reusable thing (Player.tscn, Enemy.tscn, HealthBar.tscn); instance scenes inside scenes.
- Autoloads (Project Settings > Globals) for true singletons only: game state, audio manager, scene transitions.

## GDScript
```gdscript
class_name Player
extends CharacterBody2D

signal health_changed(value: int)

@export var speed: float = 220.0
@export var max_health: int = 5
@onready var sprite: AnimatedSprite2D = $AnimatedSprite2D

var health: int = max_health:
    set(v):
        health = clampi(v, 0, max_health)
        health_changed.emit(health)

func _physics_process(delta: float) -> void:
    var dir := Input.get_vector("move_left", "move_right", "move_up", "move_down")
    velocity = dir * speed
    move_and_slide()
```
- Use static types everywhere (`var x: int`, `-> void`); enable "untyped declaration" warnings - typed code is faster and catches errors.
- Signals up, calls down: children emit signals, parents call methods on children. Connect in code with `signal.connect(callable)`.
- `_physics_process` for movement/physics, `_process` for visuals. Cache node references with `@onready`.
- Never hard-code long node paths across scenes; expose `@export var target: Node` or use groups (`add_to_group`, `get_tree().get_nodes_in_group`).
- Resources (`extends Resource` with `class_name`) for data definitions (items, stats); save as `.tres`.
- Scene changes: `get_tree().change_scene_to_file("res://...")` or `change_scene_to_packed`; for loading screens use `ResourceLoader.load_threaded_request`.

## Input and UI
- Define actions in Input Map; read with `Input.is_action_just_pressed("jump")` or `_unhandled_input`.
- UI: Control nodes with containers (VBoxContainer, MarginContainer, GridContainer) instead of manual positions; anchors presets for responsive layout; a Theme resource for fonts/colors/styles applied at the root.
- Stretch settings: Display > Window > Stretch Mode `canvas_items` (or `viewport` for pixel art) with aspect `expand`/`keep`.
- Focus navigation for gamepad: set `focus_neighbor_*` or rely on automatic neighbors; call `grab_focus()` on the first button.

## Performance
- Profiler and Monitors in the debugger; Visual Profiler for rendering.
- Use `MultiMeshInstance` for many identical objects, object pools for bullets, avoid creating nodes every frame.
- Physics: proper collision layers/masks; `Area2D` monitoring only where needed.

## Command line
```
godot --headless --path . --export-release "Windows Desktop" build/game.exe
godot --headless --path . --export-debug "Linux/X11" build/game.x86_64
godot --headless --path . -s res://tools/my_script.gd   # run a script (extends SceneTree or MainLoop)
```
- Export templates must be installed for the exact engine version (Editor > Manage Export Templates, or unpack into the templates folder).
- Preset names come from `export_presets.cfg`. Check the exit code and that the output file exists.
- C# projects need the .NET build of Godot and the .NET SDK; build with `dotnet build` before export.
