"""Video rendering and character kit package."""
from .character import (
    CharFrameSpan,
    build_char_filter_branch,
    build_char_timeline,
    ensure_composite_png,
    prepare_scene_character_concat,
)
from .kit import CharacterKit, KitError, PoseSpec, load_kit, resolve_character_kit, validate_kit
from .scene import SceneRenderResult, build_scene_ass, render_project_scenes, render_scene
from .timeline_v2 import TIMELINE_V2_VERSION, build_timeline_v2, upgrade_project_timeline

__all__ = [
    "CharacterKit",
    "KitError",
    "PoseSpec",
    "load_kit",
    "resolve_character_kit",
    "validate_kit",
    "CharFrameSpan",
    "build_char_timeline",
    "ensure_composite_png",
    "prepare_scene_character_concat",
    "build_char_filter_branch",
    "TIMELINE_V2_VERSION",
    "build_timeline_v2",
    "upgrade_project_timeline",
    "SceneRenderResult",
    "build_scene_ass",
    "render_scene",
    "render_project_scenes",
]