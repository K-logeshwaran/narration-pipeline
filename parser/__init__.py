"""Script parser package (spec v1.1 + layered v3)."""
from .config import (
    canonical_channel,
     canonical_character,
    load_channel_config,
    load_config_for_script,
    resolve_layered_config,
)
from .parser import parse_file, parse_text
from .schema import Diagnostic, ParseResult, Project
from .validators import render_report, validate

__all__ = [
    "canonical_channel",
    "canonical_character",
    "load_channel_config",
    "load_config_for_script",
    "resolve_layered_config",
    "parse_file",
    "parse_text",
    "validate",
    "render_report",
    "Diagnostic",
    "ParseResult",
    "Project",
]