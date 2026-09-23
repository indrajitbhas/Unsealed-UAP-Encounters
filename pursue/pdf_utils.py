"""PDF helpers: page count, text-layer detection, per-page rasterisation.

Uses PyMuPDF (``fitz``) when available and degrades gracefully otherwise so the
module can be imported for inspection without the dependency installed.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def _fitz():
    try:
        import fitz  # PyMuPDF
        return fitz
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "PyMuPDF (pymupdf) is required for PDF operations. "
            "Install with: pip install pymupdf"
        ) from exc


def page_count(pdf_path: Path) -> int:
    fitz = _fitz()
    with fitz.open(pdf_path) as doc:
        return doc.page_count


def has_text_layer(pdf_path: Path, sample_pages: int = 5,
                   min_chars: int = 40) -> bool:
    """Heuristic: sample the first ``sample_pages`` pages; if their combined
    extractable text exceeds ``min_chars`` the PDF is deemed to have a text
    layer (i.e. it is *not* a pure scan needing OCR)."""
    fitz = _fitz()
    total = 0
    with fitz.open(pdf_path) as doc:
        for i in range(min(sample_pages, doc.page_count)):
            total += len(doc.load_page(i).get_text("text").strip())
            if total >= min_chars:
                return True
    return total >= min_chars


def extract_text_layer(pdf_path: Path, first: int, last: int) -> List[str]:
    """Return the embedded text for pages ``first..last`` (1-based, inclusive)."""
    fitz = _fitz()
    out: List[str] = []
    with fitz.open(pdf_path) as doc:
        for pnum in range(first, last + 1):
            if pnum - 1 < doc.page_count:
                out.append(doc.load_page(pnum - 1).get_text("text"))
    return out


def render_page_png(pdf_path: Path, page_number: int, out_path: Path,
                    dpi: int = 300) -> Path:
    """Rasterise a single 1-based page to PNG (used by the Tesseract engine)."""
    fitz = _fitz()
    with fitz.open(pdf_path) as doc:
        page = doc.load_page(page_number - 1)
        pix = page.get_pixmap(dpi=dpi)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        pix.save(out_path)
    return out_path


def page_ranges(total_pages: int, already_done: set, bundle_size: int) -> List[List[int]]:
    """Compute the list of page bundles still to process.

    Skips pages already in ``already_done`` (idempotency) and groups the
    remaining pages into contiguous-ish chunks of at most ``bundle_size``.
    Returns a list of bundles, each a list of 1-based page numbers.
    """
    remaining = [p for p in range(1, total_pages + 1) if p not in already_done]
    return [remaining[i:i + bundle_size]
            for i in range(0, len(remaining), bundle_size)]
