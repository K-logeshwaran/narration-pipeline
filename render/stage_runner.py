"""Subprocess stage runner with exact per-stage Peak RSS telemetry and memory preflight.

Why this exists:
  PyTorch/Kokoro allocates ~1.3-1.4 GiB of CPU heap pages that glibc/libtorch
  do not return to the OS on `gc.collect()`. Furthermore, any child process
  forked from a 1.4 GiB parent inherits the parent's `hiwater_rss` in Linux,
  corrupting `RUSAGE_CHILDREN` measurements.

  By running each pipeline stage in a fresh subprocess spawned from a lightweight
  orchestrator (~25 MiB RSS):
    1. 100% of stage memory is returned to the OS kernel when the stage exits.
    2. `os.wait4(pid, 0)` + `/proc/<pid>` tree sampling measures the true Peak
       RSS of that stage alone (including any concurrent child workers like
       Rhubarb or FFmpeg).
"""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Callable, Optional, Sequence, Set

DEFAULT_MIN_MEM_AVAILABLE_MIB = 2048.0
MEASURED_SCENE_RENDER_PEAK_MIB = 910.0


@dataclass(frozen=True)
class StageMetrics:
    name: str
    elapsed_s: float
    wait4_maxrss_mib: float
    proc_tree_peak_mib: float
    peak_rss_mib: float
    returncode: int


def read_mem_available_mib(meminfo_path: Path | str = "/proc/meminfo") -> Optional[float]:
    """Read MemAvailable in MiB from /proc/meminfo (returns None if unavailable)."""
    p = Path(meminfo_path)
    if not p.exists():
        return None
    try:
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    return round(int(parts[1]) / 1024.0, 1)
    except OSError:
        pass
    return None


def check_memory_preflight(
    min_mib: float = DEFAULT_MIN_MEM_AVAILABLE_MIB,
    log: Callable[[str], None] = print,
    meminfo_path: Path | str = "/proc/meminfo",
) -> Optional[str]:
    """Warn (without aborting) if MemAvailable is below min_mib (~2 GiB)."""
    avail_mib = read_mem_available_mib(meminfo_path)
    if avail_mib is not None and avail_mib < min_mib:
        msg = (
            f"Low available memory ({avail_mib:.0f} MiB available < {min_mib:.0f} MiB recommended): "
            f"close heavy apps before building."
        )
        log(f"WARNING: {msg}")
        return msg
    return None


def compute_scene_workers(
    mem_available_mib: Optional[float] = None,
    per_scene_peak_mib: float = MEASURED_SCENE_RENDER_PEAK_MIB,
    hard_max: int = 1,
) -> int:
    """Derive scene render worker count from MemAvailable and measured per-scene peak,
    clamped to [1, hard_max]. Hard maximum is 1 (sequential) until proven otherwise."""
    if mem_available_mib is None:
        mem_available_mib = read_mem_available_mib()
    if mem_available_mib is None or per_scene_peak_mib <= 0:
        return 1
    usable_mib = max(mem_available_mib - 512.0, per_scene_peak_mib)
    derived = max(1, int(usable_mib // per_scene_peak_mib))
    return max(1, min(derived, int(hard_max)))


def _read_proc_status_kb(pid: int) -> tuple[int, int]:
    """Read (VmRSS_kB, VmHWM_kB) from /proc/<pid>/status."""
    rss_kb = 0
    hwm_kb = 0
    try:
        text = Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="ignore")
        for line in text.splitlines():
            if line.startswith("VmRSS:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    rss_kb = int(parts[1])
            elif line.startswith("VmHWM:"):
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    hwm_kb = int(parts[1])
    except OSError:
        pass
    return rss_kb, hwm_kb


def _descendant_pids(root_pid: int) -> Set[int]:
    """Return {root_pid} union all recursive child PIDs via /proc/<pid>/task/*/children."""
    visited: Set[int] = set()
    stack = [root_pid]
    while stack:
        pid = stack.pop()
        if pid in visited:
            continue
        visited.add(pid)
        task_dir = Path(f"/proc/{pid}/task")
        try:
            if not task_dir.is_dir():
                continue
            for tid_dir in task_dir.iterdir():
                children_file = tid_dir / "children"
                try:
                    raw = children_file.read_text(encoding="utf-8", errors="ignore")
                    for tok in raw.split():
                        if tok.isdigit():
                            cpid = int(tok)
                            if cpid not in visited:
                                stack.append(cpid)
                except OSError:
                    continue
        except OSError:
            continue
    return visited


def _sample_tree_rss_kb(root_pid: int) -> int:
    """Sum current VmRSS across root_pid and all its active descendants."""
    pids = _descendant_pids(root_pid)
    total_rss_kb = 0
    for pid in pids:
        rss_kb, hwm_kb = _read_proc_status_kb(pid)
        total_rss_kb += rss_kb if rss_kb > 0 else hwm_kb
    return total_rss_kb


def run_stage_subprocess(
    name: str,
    cmd: Sequence[str],
    *,
    cwd: Optional[Path] = None,
    quiet_output: bool = False,
    poll_interval_s: float = 0.02,
) -> StageMetrics:
    """Run `cmd` in an isolated child process, measure its true Peak RSS via
    both `os.wait4` and `/proc` tree sampling, and raise RuntimeError on non-zero exit."""
    stdout_target = subprocess.DEVNULL if quiet_output else None
    stderr_pipe = subprocess.PIPE

    t0 = time.perf_counter()
    proc = subprocess.Popen(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        stdout=stdout_target,
        stderr=stderr_pipe,
        text=True,
    )

    peak_tree_kb = 0
    stop_event = threading.Event()

    def _monitor() -> None:
        nonlocal peak_tree_kb
        while not stop_event.is_set():
            cur_kb = _sample_tree_rss_kb(proc.pid)
            if cur_kb > peak_tree_kb:
                peak_tree_kb = cur_kb
            stop_event.wait(poll_interval_s)

    monitor_thread = threading.Thread(target=_monitor, daemon=True)
    monitor_thread.start()

    stderr_text = proc.stderr.read() if proc.stderr is not None else ""

    wait4_kb = 0
    returncode = 0
    try:
        _, status, rusage = os.wait4(proc.pid, 0)
        wait4_kb = int(getattr(rusage, "ru_maxrss", 0))
        if hasattr(os, "waitstatus_to_exitcode"):
            returncode = os.waitstatus_to_exitcode(status)
        else:
            returncode = status >> 8
        proc.returncode = returncode
    except ChildProcessError:
        returncode = proc.wait()

    stop_event.set()
    monitor_thread.join(timeout=1.0)
    elapsed_s = time.perf_counter() - t0

    if not quiet_output and stderr_text:
        sys.stderr.write(stderr_text)

    if returncode != 0:
        tail = "\n".join(stderr_text.strip().splitlines()[-10:])
        raise RuntimeError(f"Stage '{name}' failed (exit {returncode}):\n{tail}")

    wait4_mib = round(wait4_kb / 1024.0, 1)
    tree_mib = round(peak_tree_kb / 1024.0, 1)
    peak_mib = round(max(wait4_mib, tree_mib), 1)
    return StageMetrics(
        name=name,
        elapsed_s=round(elapsed_s, 3),
        wait4_maxrss_mib=wait4_mib,
        proc_tree_peak_mib=tree_mib,
        peak_rss_mib=peak_mib,
        returncode=returncode,
    )