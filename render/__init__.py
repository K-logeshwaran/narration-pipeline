"""Video rendering and character kit package.

Uses PEP 562 lazy attribute loading so running `python3 -m render.scene`,
`python3 -m render.kit`, or `python3 -m render.assemble` does not import
submodules into `sys.modules` prior to `runpy` execution.
"""
from __future__ import annotations

import importlib
from typing import Any

_LAZY_EXPORTS = {
    "CharacterKit": (".kit", "CharacterKit"),
    "KitError": (".kit", "KitError"),
    "PoseSpec": (".kit", "PoseSpec"),
    "load_kit": (".kit", "load_kit"),
    "resolve_character_kit": (".kit", "resolve_character_kit"),
    "validate_kit": (".kit", "validate_kit"),
    "CharFrameSpan": (".character", "CharFrameSpan"),
    "build_char_timeline": (".character", "build_char_timeline"),
    "ensure_composite_png": (".character", "ensure_composite_png"),
    "prepare_scene_character_concat": (".character", "prepare_scene_character_concat"),
    "build_char_filter_branch": (".character", "build_char_filter_branch"),
    "TIMELINE_V2_VERSION": (".timeline_v2", "TIMELINE_V2_VERSION"),
    "build_timeline_v2": (".timeline_v2", "build_timeline_v2"),
    "upgrade_project_timeline": (".timeline_v2", "upgrade_project_timeline"),
    "SceneRenderResult": (".scene", "SceneRenderResult"),
    "build_scene_ass": (".scene", "build_scene_ass"),
    "render_scene": (".scene", "render_scene"),
    "render_project_scenes": (".scene", "render_project_scenes"),
    "StageMetrics": (".stage_runner", "StageMetrics"),
    "run_stage_subprocess": (".stage_runner", "run_stage_subprocess"),
    "read_mem_available_mib": (".stage_runner", "read_mem_available_mib"),
    "check_memory_preflight": (".stage_runner", "check_memory_preflight"),
    "compute_scene_workers": (".stage_runner", "compute_scene_workers"),
    "AssembleResult": (".assemble", "AssembleResult"),
    "assemble_project_video": (".assemble", "assemble_project_video"),
    "verify_av_sync": (".assemble", "verify_av_sync"),
}

__all__ = list(_LAZY_EXPORTS.keys())


def __getattr__(name: str) -> Any:
    if name in _LAZY_EXPORTS:
        mod_name, attr_name = _LAZY_EXPORTS[name]
        mod = importlib.import_module(mod_name, __name__)
        val = getattr(mod, attr_name)
        globals()[name] = val
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")