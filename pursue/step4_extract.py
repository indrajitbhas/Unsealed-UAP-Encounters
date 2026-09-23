"""Step 4 — Bundled OCR extraction loop with human-in-the-loop checkpoints.

Processes only the *documents* bundle. For each PDF that needs OCR:

* pages are processed in dynamic-size bundles (default N=50),
* after EVERY bundle the operator sees a quality sample + cost, and chooses:
  continue / resize N / switch engine / stop,
* every processed page is written to the idempotent ledger so re-runs never
  reprocess it,
* the spend cap is enforced *before* each bundle (projected cost).

Per document outputs (written to ``processed/``):
  * ``<file_id>.md``   — page-marked markdown (for future citations),
  * ``<file_id>.json`` — metadata (file_id, tranche, agency, incident
    date/location, page count, token_count, ocr_quality flag, entities).

The loop is resumable: stopping and re-running continues from the ledger.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings, PRICING, OCR_CONFIDENCE_FLOOR, CHARS_PER_TOKEN
from .azure_clients import BlobStore, DocIntelClient, OcrResult
from .engines import build_engine, ENGINE_DI, ENGINE_TESSERACT, OcrEngine
from .state import PageLedger, PageRecord, SpendTracker, SpendCapExceeded
from .checkpoint import (CheckpointDecision, PageSample, prompt_decision,
                         render_bundle_summary)
from .extract_entities import extract_entities, Entities
from . import pdf_utils
from .step3_inventory import InventoryRow

log = logging.getLogger(__name__)


@dataclass
class DocOutput:
    file_id: str
    tranche: str
    file_name: str
    agency: str
    incident_date: str
    location: str
    page_count: int
    pages_processed: int
    token_count: int
    mean_confidence: float
    ocr_quality_flag: str          # "ok" | "low" | "mixed"
    engine_last: str
    entities: Dict[str, List[str]]
    md_path: str
    generated_at: str


@dataclass
class ExtractionResult:
    documents: List[DocOutput]
    pages_processed_this_run: int
    stopped_reason: str            # "completed" | "operator_stop" | "spend_cap"
    engine_final: str
    bundle_size_final: int


def _token_estimate(text: str) -> int:
    return int(len(text) / CHARS_PER_TOKEN)


def _quality_flag(mean_conf: float, low_pages: int, total_pages: int) -> str:
    if mean_conf < 0:
        return "unknown"
    if mean_conf >= OCR_CONFIDENCE_FLOOR and low_pages == 0:
        return "ok"
    if low_pages == total_pages:
        return "low"
    return "mixed"


class MarkdownWriter:
    """Appends page-marked markdown so a stopped/resumed run keeps building the
    same document without duplicating already-written pages."""

    def __init__(self, path: Path, file_name: str):
        self.path = path
        if not path.exists():
            path.write_text(f"# {file_name}\n\n"
                            f"<!-- PURSUE R01 extraction — page-marked OCR output -->\n\n",
                            encoding="utf-8")

    def append_page(self, page_number: int, text: str, confidence: float) -> None:
        conf = f"{confidence:.3f}" if confidence >= 0 else "n/a"
        block = (f"\n<!-- PAGE {page_number} | confidence={conf} -->\n"
                 f"### [page {page_number}]\n\n{text.rstrip()}\n")
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(block)


def _make_di_client(settings: Settings) -> DocIntelClient:
    return DocIntelClient(settings.di_endpoint, settings.di_key,
                          dry_run=settings.dry_run)


def _di_free_pages_remaining(spend: SpendTracker, settings: Settings) -> int:
    """How many DI pages are still within the F0 free monthly allowance.

    We approximate: pages already billed at the DI per-page rate are counted
    against the 500/month free pool first (they cost $0 in reality on F0, but
    we still project S0 pricing for the cap once the pool is exhausted)."""
    di_spent = spend.by_service().get("documentintelligence", 0.0)
    billed_pages = int(round(di_spent / PRICING.di_read_per_page)) if PRICING.di_read_per_page else 0
    return max(0, PRICING.di_free_pages_per_month - billed_pages)


def _project_bundle_cost(engine: OcrEngine, n_pages: int, spend: SpendTracker,
                         settings: Settings) -> float:
    """Projected Azure cost for OCR'ing ``n_pages`` with ``engine``.

    F0 free tier: pages within the remaining 500/month allowance are free;
    beyond that we project S0 per-page pricing (the realistic upgrade path).
    Tesseract is local -> $0.
    """
    if engine.name != ENGINE_DI:
        return 0.0
    free_left = _di_free_pages_remaining(spend, settings)
    billable = max(0, n_pages - free_left)
    return round(billable * PRICING.di_read_per_page, 6)


def _samples_from_result(result: OcrResult, k: int = 3) -> List[PageSample]:
    pages = result.pages
    if not pages:
        return []
    # first, middle, last for a representative view
    idxs = sorted(set([0, len(pages) // 2, len(pages) - 1]))[:k]
    return [PageSample(pages[i].page_number, pages[i].confidence, pages[i].text)
            for i in idxs]


def _process_document(
    *, inv: InventoryRow, settings: Settings, ledger: PageLedger,
    spend: SpendTracker, engine: OcrEngine, bundle_size: int,
    di_client: DocIntelClient,
) -> tuple:
    """Process one PDF through bundles with checkpoints.

    Returns (DocOutput | None, control) where control is one of
    "continue" | "operator_stop" | "spend_cap", plus possibly-updated
    engine and bundle_size via the returned tuple.
    """
    pdf_path = Path(inv.rel_path)
    # rel_path is relative to the extracted dir; resolve against raw store
    abs_pdf = _resolve_pdf(inv, settings)
    if abs_pdf is None or not abs_pdf.exists():
        log.error("Cannot locate PDF for %s (%s); skipping.", inv.file_name, inv.file_id)
        return None, ("continue", engine, bundle_size)

    total_pages = inv.page_count or pdf_utils.page_count(abs_pdf)
    done = ledger.processed_pages_for(inv.file_id)
    md_path = settings.processed_dir / f"{inv.file_id}.md"
    writer = MarkdownWriter(md_path, inv.file_name)

    all_entities = Entities()
    conf_sum = 0.0
    conf_count = 0
    low_pages = 0
    tokens = 0
    engine_last = engine.name

    control = "continue"
    while True:
        bundles = pdf_utils.page_ranges(total_pages, ledger.processed_pages_for(inv.file_id),
                                        bundle_size)
        if not bundles:
            log.info("Document %s fully processed.", inv.file_name)
            break

        bundle_pages = bundles[0]
        bundle_id = f"{inv.file_id}:{bundle_pages[0]}-{bundle_pages[-1]}"

        # --- spend cap check BEFORE spending ---
        projected = _project_bundle_cost(engine, len(bundle_pages), spend, settings)
        try:
            spend.check_projection(projected, context=f"bundle {bundle_id}")
        except SpendCapExceeded as exc:
            log.warning("SPEND CAP: %s", exc)
            control = "spend_cap"
            break

        # --- OCR the bundle ---
        try:
            result = engine.ocr_pages(abs_pdf, bundle_pages)
        except Exception as exc:  # noqa: BLE001
            log.error("OCR failed for %s pages %s: %s", inv.file_name,
                      bundle_id, exc)
            control = "operator_stop"
            break

        # record spend (real F0 pages are free but we log the projected cost)
        if projected > 0:
            spend.record("documentintelligence", projected,
                         f"OCR {len(bundle_pages)}p {bundle_id}")

        # --- persist pages + ledger + markdown ---
        bundle_low = 0
        bundle_conf_vals = []
        for pg in result.pages:
            if ledger.is_processed(inv.file_id, pg.page_number):
                continue
            writer.append_page(pg.page_number, pg.text, pg.confidence)
            ent = extract_entities(pg.text)
            all_entities = all_entities.merge(ent)
            tk = _token_estimate(pg.text)
            tokens += tk
            if pg.confidence >= 0:
                conf_sum += pg.confidence
                conf_count += 1
                bundle_conf_vals.append(pg.confidence)
                if pg.confidence < OCR_CONFIDENCE_FLOOR:
                    low_pages += 1
                    bundle_low += 1
            ledger.record(PageRecord(
                file_id=inv.file_id, page=pg.page_number, engine=engine.name,
                confidence=pg.confidence, char_count=pg.char_count,
                token_estimate=tk, bundle_id=bundle_id,
            ))
        engine_last = engine.name

        # --- checkpoint ---
        bundle_mean = (round(sum(bundle_conf_vals) / len(bundle_conf_vals), 4)
                       if bundle_conf_vals else -1.0)
        summary = render_bundle_summary(
            bundle_id=bundle_id, file_id=inv.file_id, engine=engine.name,
            pages_in_bundle=len(result.pages),
            samples=_samples_from_result(result),
            mean_confidence=bundle_mean, low_conf_pages=bundle_low,
            cost_to_date=spend.total(), bundle_cost=projected,
            projected_total=spend.projected_total(
                _project_bundle_cost(engine, bundle_size, spend, settings)),
            spend_cap=settings.spend_cap_usd,
        )
        print(summary)
        log.info("Checkpoint after bundle %s (mean_conf=%.3f, low=%d)",
                 bundle_id, bundle_mean, bundle_low)

        decision = prompt_decision(current_bundle_size=bundle_size,
                                   current_engine=engine.name,
                                   auto_approve=settings.auto_approve)
        if decision.action == "stop":
            control = "operator_stop"
            break
        if decision.action == "resize":
            bundle_size = decision.bundle_size
            log.info("Operator resized bundle -> %d pages", bundle_size)
            continue
        if decision.action == "engine":
            engine = build_engine(
                decision.engine, di_client=di_client,
                per_page_cost=PRICING.di_read_per_page, dry_run=settings.dry_run)
            log.info("Operator switched engine -> %s", engine.name)
            continue
        # continue -> loop for next bundle

    mean_conf = round(conf_sum / conf_count, 4) if conf_count else -1.0
    pages_done_now = len(ledger.processed_pages_for(inv.file_id))
    doc = DocOutput(
        file_id=inv.file_id, tranche="R01", file_name=inv.file_name,
        agency=inv.agency, incident_date=inv.incident_date, location=inv.location,
        page_count=total_pages, pages_processed=pages_done_now,
        token_count=tokens, mean_confidence=mean_conf,
        ocr_quality_flag=_quality_flag(mean_conf, low_pages, max(1, conf_count)),
        engine_last=engine_last, entities=all_entities.to_dict(),
        md_path=str(md_path),
        generated_at=datetime.now(timezone.utc).isoformat(),
    )
    (settings.processed_dir / f"{inv.file_id}.json").write_text(
        json.dumps(asdict(doc), indent=2), encoding="utf-8")

    # upload processed artifacts to blob (best-effort)
    return doc, (control, engine, bundle_size)


def _resolve_pdf(inv: InventoryRow, settings: Settings) -> Optional[Path]:
    """Find the PDF on disk given an inventory row.

    Inventory rel_path is relative to a bundle's extracted dir; search the raw
    store for a matching relative path or file name."""
    raw = settings.raw_dir
    candidate = raw / inv.bundle / inv.rel_path
    if candidate.exists():
        return candidate
    # fall back to name search
    matches = list(raw.rglob(inv.file_name))
    return matches[0] if matches else None


def run(settings: Settings, inventory: List[InventoryRow], *,
        blob_store: Optional[BlobStore] = None) -> ExtractionResult:
    """Execute Step 4 over all document PDFs that need OCR."""
    settings.ensure_dirs()
    ledger = PageLedger(settings.ledger_path)
    spend = SpendTracker(settings.spend_ledger_path, settings.spend_cap_usd)
    di_client = _make_di_client(settings)

    engine = build_engine(ENGINE_DI, di_client=di_client,
                          per_page_cost=PRICING.di_read_per_page,
                          dry_run=settings.dry_run)
    bundle_size = settings.bundle_size

    # Only documents-kind PDFs are OCR'd. Videos are inventory-only.
    docs = [r for r in inventory
            if r.kind == "documents" and r.ext == ".pdf"]
    # Prefer scanned docs (no text layer) first — those truly need OCR.
    docs.sort(key=lambda r: (r.has_text_layer is True, r.file_name))

    outputs: List[DocOutput] = []
    stopped_reason = "completed"
    pages_before = ledger.total_pages()

    for inv in docs:
        doc, (control, engine, bundle_size) = _process_document(
            inv=inv, settings=settings, ledger=ledger, spend=spend,
            engine=engine, bundle_size=bundle_size, di_client=di_client)
        if doc is not None:
            outputs.append(doc)
            if blob_store is not None:
                _upload_doc_outputs(blob_store, settings, doc)
        if control in ("operator_stop", "spend_cap"):
            stopped_reason = control
            log.warning("Extraction halted: %s", control)
            break

    pages_after = ledger.total_pages()
    result = ExtractionResult(
        documents=outputs,
        pages_processed_this_run=pages_after - pages_before,
        stopped_reason=stopped_reason,
        engine_final=engine.name,
        bundle_size_final=bundle_size,
    )
    (settings.state_dir / "extraction_result.json").write_text(
        json.dumps({
            "pages_processed_this_run": result.pages_processed_this_run,
            "stopped_reason": result.stopped_reason,
            "engine_final": result.engine_final,
            "bundle_size_final": result.bundle_size_final,
            "documents": [asdict(d) for d in outputs],
        }, indent=2), encoding="utf-8")
    return result


def _upload_doc_outputs(blob_store: BlobStore, settings: Settings, doc: DocOutput) -> None:
    try:
        blob_store.upload_file(settings.container_processed,
                               f"pursue/r01/{doc.file_id}.md",
                               Path(doc.md_path), overwrite=True)
        blob_store.upload_file(settings.container_processed,
                               f"pursue/r01/{doc.file_id}.json",
                               settings.processed_dir / f"{doc.file_id}.json",
                               overwrite=True)
    except Exception as exc:  # noqa: BLE001
        log.warning("Processed upload failed for %s: %s", doc.file_id, exc)
