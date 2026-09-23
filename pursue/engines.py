"""Pluggable OCR engines with a single interface.

Both engines take a PDF path + a list of 1-based page numbers and return an
:class:`~pursue.azure_clients.OcrResult`. This lets the extraction loop switch
engines per bundle at a human checkpoint without any other code changing.

Engines
-------
* ``documentintelligence`` — Azure DI prebuilt-read (billable).
* ``tesseract``            — local Tesseract via pytesseract (free, no Azure).
"""
from __future__ import annotations

import logging
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import List

from .azure_clients import DocIntelClient, OcrPage, OcrResult
from . import pdf_utils

log = logging.getLogger(__name__)

ENGINE_DI = "documentintelligence"
ENGINE_TESSERACT = "tesseract"


class OcrEngine(ABC):
    name: str = "base"
    #: per-page Azure cost used for spend projection (0 for local engines)
    per_page_cost: float = 0.0

    @abstractmethod
    def ocr_pages(self, pdf_path: Path, pages: List[int]) -> OcrResult:
        ...


class DocumentIntelligenceEngine(OcrEngine):
    name = ENGINE_DI

    def __init__(self, client: DocIntelClient, per_page_cost: float):
        self.client = client
        self.per_page_cost = per_page_cost

    def ocr_pages(self, pdf_path: Path, pages: List[int]) -> OcrResult:
        # DI accepts a page-range spec; send a compact comma/hyphen list.
        spec = _pages_to_spec(pages)
        log.info("DI OCR %s pages=%s", pdf_path.name, spec)
        return self.client.analyze_pages(pdf_path, spec)


class TesseractEngine(OcrEngine):
    name = ENGINE_TESSERACT
    per_page_cost = 0.0

    def __init__(self, dpi: int = 300, dry_run: bool = False):
        self.dpi = dpi
        self.dry_run = dry_run

    def ocr_pages(self, pdf_path: Path, pages: List[int]) -> OcrResult:
        if self.dry_run:
            # Reuse the DI synthetic generator for consistent dry-run behaviour.
            from .azure_clients import _synthetic_ocr
            spec = f"{min(pages)}-{max(pages)}"
            res = _synthetic_ocr(pdf_path, spec)
            # keep only requested pages
            res.pages = [p for p in res.pages if p.page_number in set(pages)]
            return res

        import pytesseract  # lazy
        from PIL import Image  # noqa: F401 (import validates availability)

        out_pages: List[OcrPage] = []
        with tempfile.TemporaryDirectory(prefix="pursue_tess_") as td:
            tmp = Path(td)
            for pnum in pages:
                png = pdf_utils.render_page_png(
                    pdf_path, pnum, tmp / f"p{pnum}.png", dpi=self.dpi)
                data = pytesseract.image_to_data(
                    str(png), output_type=pytesseract.Output.DICT)
                text = pytesseract.image_to_string(str(png))
                confs = [int(c) for c in data.get("conf", [])
                         if str(c).lstrip("-").isdigit() and int(c) >= 0]
                mean_conf = round(sum(confs) / len(confs) / 100.0, 4) if confs else -1.0
                out_pages.append(OcrPage(pnum, text, mean_conf))
        return OcrResult(out_pages)


def _pages_to_spec(pages: List[int]) -> str:
    """Compress a sorted page list into a DI page-range spec, e.g.
    [1,2,3,5] -> '1-3,5'."""
    if not pages:
        return ""
    pages = sorted(set(pages))
    ranges = []
    start = prev = pages[0]
    for p in pages[1:]:
        if p == prev + 1:
            prev = p
            continue
        ranges.append((start, prev))
        start = prev = p
    ranges.append((start, prev))
    return ",".join(f"{a}" if a == b else f"{a}-{b}" for a, b in ranges)


def build_engine(name: str, *, di_client: DocIntelClient, per_page_cost: float,
                 dry_run: bool) -> OcrEngine:
    if name == ENGINE_DI:
        return DocumentIntelligenceEngine(di_client, per_page_cost)
    if name == ENGINE_TESSERACT:
        return TesseractEngine(dry_run=dry_run)
    raise ValueError(f"Unknown engine: {name}")
