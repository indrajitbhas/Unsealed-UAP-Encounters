"""Video inventory helpers — ffprobe only. No transcription, ever.

Per the probe rules, videos are inventoried (count, sha256, duration) but never
transcribed. This module shells out to ``ffprobe`` and returns durations.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".m4v", ".mpg", ".mpeg", ".webm"}


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def probe_duration_seconds(path: Path) -> Optional[float]:
    """Return the media duration in seconds, or None if it can't be determined."""
    if not ffprobe_available():
        log.warning("ffprobe not found on PATH; cannot probe %s", path.name)
        return None
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", str(path),
    ]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        log.error("ffprobe timed out on %s", path.name)
        return None
    if out.returncode != 0:
        log.error("ffprobe failed on %s: %s", path.name, out.stderr.strip())
        return None
    try:
        data = json.loads(out.stdout)
        dur = data.get("format", {}).get("duration")
        return float(dur) if dur is not None else None
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        log.error("Could not parse ffprobe output for %s: %s", path.name, exc)
        return None


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTS
