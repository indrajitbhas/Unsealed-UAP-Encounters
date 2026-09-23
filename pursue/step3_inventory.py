"""Step 3 — Inventory every acquired file.

Per file we record:
  * file_id (stable, derived from relative path),
  * bundle / kind (documents | videos),
  * sha256, size, mime-ish type, extension,
  * PDF: page_count, has_text_layer,
  * video: duration_seconds (ffprobe),
then join with the portal records DB (agency / incident_date / location / type)
and write ``inventory.csv``.
"""
from __future__ import annotations

import csv
import hashlib
import logging
import mimetypes
from dataclasses import dataclass, asdict, fields
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings
from . import media_utils, pdf_utils

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024


@dataclass
class InventoryRow:
    file_id: str
    bundle: str
    kind: str
    file_name: str
    rel_path: str
    ext: str
    mime: str
    size_bytes: int
    sha256: str
    page_count: Optional[int] = None
    has_text_layer: Optional[bool] = None
    duration_seconds: Optional[float] = None
    # joined from portal records DB
    agency: str = ""
    incident_date: str = ""
    location: str = ""
    type: str = ""


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _load_portal_metadata(path: Path) -> Dict[str, dict]:
    """Return {file_name(lower): row} from the portal records CSV."""
    meta: Dict[str, dict] = {}
    if not path.exists():
        log.warning("No portal metadata at %s; inventory will lack join fields.", path)
        return meta
    with path.open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            key = (row.get("file_name") or "").strip().lower()
            if key:
                meta[key] = {k: (v or "").strip() for k, v in row.items()}
    log.info("Loaded %d portal metadata rows.", len(meta))
    return meta


def _iter_files(root: Path):
    for p in sorted(root.rglob("*")):
        if p.is_file() and not p.name.endswith((".manifest.json", ".part")):
            yield p


def inventory_file(path: Path, *, bundle: str, kind: str, root: Path) -> InventoryRow:
    rel = path.relative_to(root)
    file_id = hashlib.sha1(str(rel).encode()).hexdigest()[:12]
    ext = path.suffix.lower()
    mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    row = InventoryRow(
        file_id=file_id, bundle=bundle, kind=kind, file_name=path.name,
        rel_path=str(rel), ext=ext, mime=mime,
        size_bytes=path.stat().st_size, sha256=_sha256(path),
    )

    if ext == ".pdf":
        try:
            row.page_count = pdf_utils.page_count(path)
            row.has_text_layer = pdf_utils.has_text_layer(path)
        except Exception as exc:  # noqa: BLE001 — inventory must be robust
            log.error("PDF inspection failed for %s: %s", path.name, exc)
    elif media_utils.is_video(path):
        row.duration_seconds = media_utils.probe_duration_seconds(path)

    return row


def run(settings: Settings, acquired: Dict[str, "object"]) -> List[InventoryRow]:
    """Execute Step 3. ``acquired`` maps bundle name -> AcquiredBundle."""
    settings.ensure_dirs()
    portal = _load_portal_metadata(settings.portal_metadata_path)

    rows: List[InventoryRow] = []
    for name, bundle in acquired.items():
        kind = getattr(bundle, "kind", "documents")
        extracted = Path(getattr(bundle, "extracted_dir"))
        if not extracted.exists():
            log.warning("Extracted dir missing for bundle %s: %s", name, extracted)
            continue
        log.info("Inventorying bundle '%s' (%s) at %s", name, kind, extracted)
        for f in _iter_files(extracted):
            row = inventory_file(f, bundle=name, kind=kind, root=extracted)
            # join portal metadata by file name
            m = portal.get(row.file_name.lower())
            if m:
                row.agency = m.get("agency", "")
                row.incident_date = m.get("incident_date", "")
                row.location = m.get("location", "")
                row.type = m.get("type", "")
            rows.append(row)

    _write_csv(settings.inventory_path, rows)
    _log_summary(rows)
    return rows


def _write_csv(path: Path, rows: List[InventoryRow]) -> None:
    cols = [f.name for f in fields(InventoryRow)]
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=cols)
        writer.writeheader()
        for r in rows:
            writer.writerow(asdict(r))
    log.info("Wrote inventory (%d files) -> %s", len(rows), path)


def _log_summary(rows: List[InventoryRow]) -> None:
    pdfs = [r for r in rows if r.ext == ".pdf"]
    vids = [r for r in rows if r.duration_seconds is not None]
    total_pages = sum(r.page_count or 0 for r in pdfs)
    scanned = sum(1 for r in pdfs if r.has_text_layer is False)
    total_minutes = sum(r.duration_seconds or 0 for r in vids) / 60.0
    log.info("Inventory summary:")
    log.info("  files             : %d", len(rows))
    log.info("  PDFs              : %d  (pages: %d)", len(pdfs), total_pages)
    log.info("  scanned (no text) : %d  -> require OCR", scanned)
    log.info("  videos            : %d  (%.1f minutes)", len(vids), total_minutes)


def load_inventory(path: Path) -> List[InventoryRow]:
    """Re-load a previously written inventory.csv (used by later steps/re-runs)."""
    rows: List[InventoryRow] = []
    if not path.exists():
        return rows
    valid = {f.name for f in fields(InventoryRow)}
    with path.open("r", encoding="utf-8", newline="") as fh:
        for d in csv.DictReader(fh):
            clean = {k: v for k, v in d.items() if k in valid}
            # coerce types
            clean["size_bytes"] = int(clean.get("size_bytes") or 0)
            clean["page_count"] = int(clean["page_count"]) if clean.get("page_count") else None
            if clean.get("has_text_layer") in ("True", "False"):
                clean["has_text_layer"] = clean["has_text_layer"] == "True"
            else:
                clean["has_text_layer"] = None
            clean["duration_seconds"] = (float(clean["duration_seconds"])
                                         if clean.get("duration_seconds") else None)
            rows.append(InventoryRow(**clean))
    return rows
