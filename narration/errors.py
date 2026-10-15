"""Errors that identify scene, segment, line and problem (spec section 27)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional


@dataclass
class Problem:
    problem: str
    scene: Optional[str] = None
    segment: Optional[str] = None
    line: Optional[int] = None
    detail: Optional[str] = None

    def format(self) -> str:
        where = []
        if self.scene:
            where.append(self.scene)
        if self.segment:
            where.append(f"narration segment {self.segment.rsplit('seg', 1)[-1]}")
        loc = " / ".join(where) or "project"
        if self.line is not None:
            loc += f" (script line {self.line})"
        out = f"ERROR:\n{loc}\n{self.problem}"
        if self.detail:
            out += f"\n{self.detail}"
        return out


class NarrationError(Exception):
    """One or more problems that stop narration. Carries every problem found."""

    def __init__(self, problems: List[Problem]):
        self.problems = problems
        super().__init__("\n\n".join(p.format() for p in problems))


class AudioToolError(Exception):
    """ffmpeg is missing or failed."""
