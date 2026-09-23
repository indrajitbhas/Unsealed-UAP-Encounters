"""Human-in-the-loop checkpoints.

The extraction loop pauses after every page bundle and presents:

* a quality sample (per-page confidence + a text excerpt),
* cost-to-date and projected total,

then asks the operator to choose one of:

* ``continue``           — process the next bundle with the current settings,
* ``resize <N>``         — change the bundle size to N pages,
* ``engine <name>``      — switch OCR engine (documentintelligence | tesseract),
* ``stop``               — halt the loop and write the report.

Two Step-1 style approval prompts (provisioning, verification) reuse
:func:`confirm`.
"""
from __future__ import annotations

import logging
import sys
from dataclasses import dataclass
from typing import List, Optional

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Decision object
# --------------------------------------------------------------------------- #

@dataclass
class CheckpointDecision:
    action: str                    # continue | resize | engine | stop
    bundle_size: Optional[int] = None
    engine: Optional[str] = None


VALID_ENGINES = {"documentintelligence", "di", "tesseract"}


def _normalize_engine(name: str) -> Optional[str]:
    name = name.strip().lower()
    if name in ("di", "documentintelligence"):
        return "documentintelligence"
    if name == "tesseract":
        return "tesseract"
    return None


# --------------------------------------------------------------------------- #
# Sample rendering
# --------------------------------------------------------------------------- #

@dataclass
class PageSample:
    page: int
    confidence: float
    excerpt: str


def render_bundle_summary(
    *,
    bundle_id: str,
    file_id: str,
    engine: str,
    pages_in_bundle: int,
    samples: List[PageSample],
    mean_confidence: float,
    low_conf_pages: int,
    cost_to_date: float,
    bundle_cost: float,
    projected_total: float,
    spend_cap: float,
    excerpt_chars: int = 400,
) -> str:
    """Build the human-readable checkpoint report shown before each prompt."""
    lines: List[str] = []
    lines.append("")
    lines.append("=" * 72)
    lines.append(f"  CHECKPOINT — bundle '{bundle_id}'  (file: {file_id})")
    lines.append("=" * 72)
    lines.append(f"  Engine:            {engine}")
    lines.append(f"  Pages in bundle:   {pages_in_bundle}")
    lines.append(f"  Mean confidence:   {mean_confidence:.3f}")
    lines.append(f"  Low-quality pages: {low_conf_pages}/{pages_in_bundle}")
    lines.append("-" * 72)
    lines.append("  QUALITY SAMPLE")
    for s in samples:
        excerpt = " ".join(s.excerpt.split())[:excerpt_chars]
        conf = f"{s.confidence:.3f}" if s.confidence >= 0 else "n/a"
        lines.append(f"   • p{s.page:<4} conf={conf}")
        lines.append(f"     {excerpt!r}")
    lines.append("-" * 72)
    lines.append("  COST")
    lines.append(f"   • This bundle:     ${bundle_cost:.4f}")
    lines.append(f"   • Cost to date:    ${cost_to_date:.4f}")
    lines.append(f"   • Projected total: ${projected_total:.4f}")
    lines.append(f"   • Spend cap:       ${spend_cap:.2f}  "
                 f"(remaining ${spend_cap - cost_to_date:.4f})")
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Prompt
# --------------------------------------------------------------------------- #

def prompt_decision(
    *,
    current_bundle_size: int,
    current_engine: str,
    auto_approve: bool = False,
) -> CheckpointDecision:
    """Read the operator's decision from stdin.

    In ``auto_approve`` mode (unattended demos/CI) it returns ``continue``.
    On EOF (non-interactive stdin) it defaults to ``stop`` — the safe choice,
    since silently spending money without a human is exactly what we must avoid.
    """
    if auto_approve:
        log.info("[auto-approve] continuing with %s @ %d pages/bundle",
                 current_engine, current_bundle_size)
        return CheckpointDecision(action="continue")

    menu = (
        "\nYour options:\n"
        "  [c] continue            process next bundle as-is\n"
        "  [r] resize <N>          set bundle size to N pages (e.g. 'r 100')\n"
        "  [e] engine <name>       switch engine: documentintelligence | tesseract\n"
        "  [s] stop                halt and write the report\n"
        f"  (current: engine={current_engine}, bundle={current_bundle_size})\n"
        "> "
    )
    while True:
        try:
            raw = input(menu).strip()
        except EOFError:
            log.warning("No interactive input available; defaulting to STOP.")
            return CheckpointDecision(action="stop")

        if not raw:
            continue
        parts = raw.split()
        cmd = parts[0].lower()

        if cmd in ("c", "continue"):
            return CheckpointDecision(action="continue")

        if cmd in ("s", "stop"):
            return CheckpointDecision(action="stop")

        if cmd in ("r", "resize"):
            if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) < 1:
                print("  ! resize needs a positive integer, e.g. 'r 100'")
                continue
            return CheckpointDecision(action="resize", bundle_size=int(parts[1]))

        if cmd in ("e", "engine"):
            if len(parts) < 2:
                print("  ! engine needs a name: documentintelligence | tesseract")
                continue
            eng = _normalize_engine(parts[1])
            if not eng:
                print("  ! unknown engine; use documentintelligence | tesseract")
                continue
            return CheckpointDecision(action="engine", engine=eng)

        print("  ! unrecognised command")


def confirm(question: str, *, auto_approve: bool = False, default: bool = False) -> bool:
    """Simple yes/no confirmation used by Step 1."""
    if auto_approve:
        log.info("[auto-approve] %s -> yes", question)
        return True
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        raw = input(question + suffix).strip().lower()
    except EOFError:
        return default
    if not raw:
        return default
    return raw in ("y", "yes")
