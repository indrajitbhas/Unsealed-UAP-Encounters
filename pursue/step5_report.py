"""Step 5 — Write ``probe_report.md``.

Summarises what the Phase-0 probe actually did and extrapolates the cost of the
full six-tranche project from the measured R01 unit economics.

Sections:
  * What was processed (files / pages / tokens / media minutes)
  * OCR quality (failure rate, low-confidence pages)
  * Actual Azure spend per service (vs the $25 cap)
  * Whole-project extrapolation across R01..R06 by document GB
  * OCR-strategy recommendation for the MVP
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .config import Settings, PRICING, TRANCHE_DOC_SIZES_GB, OCR_CONFIDENCE_FLOOR
from .state import PageLedger, SpendTracker
from .step3_inventory import InventoryRow
from .step4_extract import ExtractionResult

log = logging.getLogger(__name__)


def _fmt_usd(x: float) -> str:
    return f"${x:,.4f}"


def _r01_unit_economics(inventory: List[InventoryRow], ledger: PageLedger,
                        spend: SpendTracker):
    """Derive per-GB cost/pages from what R01 actually measured."""
    doc_rows = [r for r in inventory if r.kind == "documents" and r.ext == ".pdf"]
    r01_doc_gb = sum(r.size_bytes for r in doc_rows) / 1e9 or TRANCHE_DOC_SIZES_GB["R01"]
    total_doc_pages = sum(r.page_count or 0 for r in doc_rows)
    pages_processed = ledger.total_pages()

    di_spend = spend.by_service().get("documentintelligence", 0.0)
    # cost per page from actual projected spend, or list price if nothing billed
    cost_per_page = (di_spend / pages_processed) if pages_processed else PRICING.di_read_per_page
    if cost_per_page <= 0:
        cost_per_page = PRICING.di_read_per_page

    pages_per_gb = (total_doc_pages / r01_doc_gb) if r01_doc_gb else 0.0
    return {
        "r01_doc_gb": r01_doc_gb,
        "total_doc_pages": total_doc_pages,
        "pages_processed": pages_processed,
        "cost_per_page": cost_per_page,
        "pages_per_gb": pages_per_gb,
        "di_spend": di_spend,
    }


def _extrapolate(econ: dict) -> List[dict]:
    """Project pages + DI cost for every tranche from R01 unit economics."""
    rows = []
    pages_per_gb = econ["pages_per_gb"]
    # If R01 measured pages/GB, use it; else fall back to a typical scan density.
    if pages_per_gb <= 0:
        pages_per_gb = 8000.0  # conservative default: ~8k pages per GB of scans
    for tranche, gb in TRANCHE_DOC_SIZES_GB.items():
        est_pages = int(round(gb * pages_per_gb))
        # apply DI list price with F0 free pool only once (first 500 pages free)
        billable = max(0, est_pages - PRICING.di_free_pages_per_month)
        est_cost = round(billable * PRICING.di_read_per_page, 2)
        rows.append({"tranche": tranche, "gb": gb, "est_pages": est_pages,
                     "est_di_cost": est_cost})
    return rows


def _ocr_recommendation(econ: dict, failure_rate: float, extrap: List[dict]) -> str:
    total_pages = sum(r["est_pages"] for r in extrap)
    total_di = sum(r["est_di_cost"] for r in extrap)
    lines = []
    lines.append(f"Across all six tranches we project **~{total_pages:,} document "
                 f"pages**. At Document Intelligence prebuilt-read S0 pricing "
                 f"(~{_fmt_usd(PRICING.di_read_per_page)}/page) that is "
                 f"**~{_fmt_usd(total_di)}** of OCR spend for the full corpus.")
    lines.append("")
    if failure_rate <= 0.05:
        lines.append(
            "- **Recommended MVP engine: Azure Document Intelligence (prebuilt-read).** "
            f"Measured OCR failure/low-confidence rate on R01 was "
            f"**{failure_rate*100:.1f}%**, comfortably below the 5% bar. DI's "
            "layout-aware output and per-word confidence justify the per-page cost "
            "for a citation-grade corpus.")
        lines.append(
            "- Use the **F0 free tier** for iterative development (500 pages/month) "
            "and switch to **S0** for production batches. Keep the page-bundle "
            "checkpoint to catch quality regressions early.")
    elif failure_rate <= 0.20:
        lines.append(
            "- **Recommended MVP engine: hybrid.** DI as the default with a "
            f"**Tesseract fallback** for the low-confidence tail "
            f"(measured {failure_rate*100:.1f}%). Route pages below "
            f"{OCR_CONFIDENCE_FLOOR:.2f} confidence to a manual/Tesseract re-run "
            "rather than paying DI twice.")
    else:
        lines.append(
            f"- **Investigate source quality first.** The measured failure rate "
            f"({failure_rate*100:.1f}%) is high enough that neither engine will "
            "produce citation-grade text without pre-processing (deskew, denoise, "
            "higher-DPI rasterisation). Pilot Tesseract locally (zero Azure cost) "
            "before committing DI budget.")
    lines.append(
        "- **Cost control:** the $25 probe cap maps to roughly "
        f"{int(25 / max(PRICING.di_read_per_page, 1e-9)):,} DI pages. Stage the "
        "full project tranche-by-tranche behind the same checkpoint + ledger so "
        "spend stays observable and resumable.")
    return "\n".join(lines)


def run(settings: Settings, *, inventory: List[InventoryRow],
        extraction: Optional[ExtractionResult]) -> Path:
    """Execute Step 5. Returns the report path."""
    ledger = PageLedger(settings.ledger_path)
    spend = SpendTracker(settings.spend_ledger_path, settings.spend_cap_usd)

    # --- aggregate measured facts ---
    doc_rows = [r for r in inventory if r.kind == "documents"]
    vid_rows = [r for r in inventory if r.duration_seconds is not None]
    files_processed = len({d.file_id for d in (extraction.documents if extraction else [])})
    pages_processed = ledger.total_pages()

    tokens = sum(rec.get("token_estimate", 0) for rec in ledger.iter_records())
    media_minutes = sum(r.duration_seconds or 0 for r in vid_rows) / 60.0

    # OCR failure rate = low-confidence pages / processed pages
    low = sum(1 for rec in ledger.iter_records()
              if 0 <= rec.get("confidence", -1) < OCR_CONFIDENCE_FLOOR)
    failure_rate = (low / pages_processed) if pages_processed else 0.0

    econ = _r01_unit_economics(inventory, ledger, spend)
    extrap = _extrapolate(econ)

    # --- render ---
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    by_svc = spend.by_service()
    total_spend = spend.total()

    md: List[str] = []
    md.append("# PURSUE R01 Extraction Probe — Report")
    md.append("")
    md.append(f"*Generated {now} · Phase-0 validation · spend cap "
              f"{_fmt_usd(settings.spend_cap_usd)}*")
    md.append("")
    if extraction:
        md.append(f"**Run outcome:** `{extraction.stopped_reason}` · "
                  f"engine `{extraction.engine_final}` · final bundle size "
                  f"{extraction.bundle_size_final} pages")
        md.append("")

    # 1. Processed
    md.append("## 1. What was processed")
    md.append("")
    md.append("| Metric | Value |")
    md.append("|---|---|")
    md.append(f"| Document files (R01) | {len(doc_rows)} |")
    md.append(f"| Document files OCR'd | {files_processed} |")
    md.append(f"| Pages processed | {pages_processed:,} |")
    md.append(f"| Tokens extracted (est.) | {tokens:,} |")
    md.append(f"| Video files inventoried | {len(vid_rows)} |")
    md.append(f"| Media minutes inventoried | {media_minutes:,.1f} |")
    md.append("")

    # 2. OCR quality
    md.append("## 2. OCR quality")
    md.append("")
    md.append(f"- Low-confidence pages (< {OCR_CONFIDENCE_FLOOR:.2f}): "
              f"**{low}** of {pages_processed:,}")
    md.append(f"- **OCR failure rate: {failure_rate*100:.1f}%**")
    md.append("")
    if extraction and extraction.documents:
        md.append("| file_id | file | pages | tokens | mean conf | quality | engine |")
        md.append("|---|---|---|---|---|---|---|")
        for d in extraction.documents:
            mc = f"{d.mean_confidence:.3f}" if d.mean_confidence >= 0 else "n/a"
            md.append(f"| `{d.file_id}` | {d.file_name} | {d.pages_processed} | "
                      f"{d.token_count:,} | {mc} | {d.ocr_quality_flag} | "
                      f"{d.engine_last} |")
        md.append("")

    # 3. Spend
    md.append("## 3. Actual Azure spend")
    md.append("")
    md.append("| Service | Spend |")
    md.append("|---|---|")
    for svc, amt in sorted(by_svc.items()):
        md.append(f"| {svc} | {_fmt_usd(amt)} |")
    if not by_svc:
        md.append("| (none billed) | $0.0000 |")
    md.append(f"| **Total** | **{_fmt_usd(total_spend)}** |")
    md.append(f"| Cap | {_fmt_usd(settings.spend_cap_usd)} |")
    md.append(f"| Remaining | {_fmt_usd(settings.spend_cap_usd - total_spend)} |")
    md.append("")
    note = ("Note: on the **F0 free tier** the first "
            f"{PRICING.di_free_pages_per_month} DI pages/month are $0; the "
            "figures above project **S0** per-page pricing beyond the free pool "
            "so the cap stays conservative.")
    md.append(note)
    md.append("")

    # 4. Extrapolation
    md.append("## 4. Whole-project extrapolation (R01–R06)")
    md.append("")
    if econ["pages_per_gb"] > 0:
        md.append(f"Measured on R01: **{econ['pages_per_gb']:,.0f} pages/GB** of "
                  f"documents, **{_fmt_usd(econ['cost_per_page'])}/page** DI cost.")
    else:
        md.append("R01 processed no pages this run; extrapolation uses a "
                  "conservative default of 8,000 pages/GB and DI list pricing.")
    md.append("")
    md.append("| Tranche | Docs (GB) | Est. pages | Est. DI cost |")
    md.append("|---|---|---|---|")
    for r in extrap:
        md.append(f"| {r['tranche']} | {r['gb']:.3f} | {r['est_pages']:,} | "
                  f"{_fmt_usd(r['est_di_cost'])} |")
    tot_gb = sum(r['gb'] for r in extrap)
    tot_pages = sum(r['est_pages'] for r in extrap)
    tot_cost = sum(r['est_di_cost'] for r in extrap)
    md.append(f"| **Total** | **{tot_gb:.3f}** | **{tot_pages:,}** | "
              f"**{_fmt_usd(tot_cost)}** |")
    md.append("")

    # 5. Recommendation
    md.append("## 5. OCR-strategy recommendation for the MVP")
    md.append("")
    md.append(_ocr_recommendation(econ, failure_rate, extrap))
    md.append("")
    md.append("---")
    md.append("")
    md.append("*Neutral extraction only — dates, places, agencies, roles. "
              "No interpretation. Videos inventoried (count, sha256, duration); "
              "not transcribed.*")

    settings.report_path.write_text("\n".join(md), encoding="utf-8")
    log.info("Wrote report -> %s", settings.report_path)
    return settings.report_path
