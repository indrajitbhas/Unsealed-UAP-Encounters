"""Step 2 — Acquire the Release 01 bundles + portal records database.

The PURSUE portal (https://www.war.gov/ufo/) renders its download links with
JavaScript, so we cannot reliably scrape direct URLs. This step therefore:

* accepts **either** direct bundle URLs **or** local zip paths (operator input),
* downloads (if URL), verifies the archive is intact, records sha256,
* stores the bundle immutably under ``raw/pursue/r01/`` and (optionally) uploads
  it to the ``raw`` blob container,
* extracts the archive into ``raw/pursue/r01/<bundle>/`` for inventory,
* captures the portal's Release-01 records database (agency, incident date,
  location, type per file). If an export cannot be scraped, the operator
  supplies a manual CSV export.

Two bundles are expected for R01:
  * documents (~1.2 GB) — processed fully in later steps,
  * videos    (~1.3 GB) — inventory only.
"""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import urllib.request
import zipfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Dict, List, Optional

from .config import Settings

log = logging.getLogger(__name__)

CHUNK = 1024 * 1024  # 1 MiB


@dataclass
class BundleSpec:
    name: str                # "documents" | "videos"
    source: str              # URL or local path
    kind: str                # "documents" | "videos"


@dataclass
class AcquiredBundle:
    name: str
    kind: str
    archive_path: str
    extracted_dir: str
    sha256: str
    size_bytes: int
    blob_url: Optional[str] = None


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _download(url: str, dest: Path) -> None:
    log.info("Downloading %s -> %s", url, dest.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "pursue-probe/0.1"})
    with urllib.request.urlopen(req, timeout=120) as resp, tmp.open("wb") as out:
        total = 0
        while True:
            chunk = resp.read(CHUNK)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
            if total % (50 * CHUNK) < CHUNK:
                log.info("   ... %.1f MB", total / 1e6)
    tmp.replace(dest)
    log.info("Downloaded %.1f MB", dest.stat().st_size / 1e6)


def _obtain_archive(spec: BundleSpec, raw_dir: Path) -> Path:
    """Return a local path to the bundle archive, downloading if it's a URL.

    Immutable-by-default: if the destination already exists we keep it (a
    re-run must not overwrite an acquired artifact) and skip re-downloading.
    """
    if spec.source.startswith(("http://", "https://")):
        fname = spec.source.split("?")[0].rstrip("/").split("/")[-1] or f"{spec.name}.zip"
        dest = raw_dir / fname
        if dest.exists():
            log.info("Archive already present (immutable), skipping download: %s",
                     dest.name)
            return dest
        _download(spec.source, dest)
        return dest

    src = Path(spec.source).expanduser().resolve()
    if not src.exists():
        raise FileNotFoundError(f"Local bundle not found: {src}")
    dest = raw_dir / src.name
    if dest.exists():
        log.info("Archive already in raw store (immutable): %s", dest.name)
        return dest
    log.info("Copying local bundle into immutable raw store: %s", src.name)
    shutil.copy2(src, dest)
    return dest


def _verify_and_extract(archive: Path, out_dir: Path) -> Path:
    """Verify the zip is intact and extract it (idempotent)."""
    if out_dir.exists() and any(out_dir.iterdir()):
        log.info("Already extracted: %s", out_dir)
        return out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    if not zipfile.is_zipfile(archive):
        # Not a zip (maybe a single PDF or already-unpacked dir); link it in.
        log.warning("%s is not a zip; treating as a single artifact.", archive.name)
        shutil.copy2(archive, out_dir / archive.name)
        return out_dir
    with zipfile.ZipFile(archive) as zf:
        bad = zf.testzip()
        if bad is not None:
            raise RuntimeError(f"Corrupt archive {archive.name}: first bad file {bad}")
        zf.extractall(out_dir)
    log.info("Extracted %s -> %s", archive.name, out_dir)
    return out_dir


def acquire_bundle(spec: BundleSpec, settings: Settings, blob_store=None) -> AcquiredBundle:
    raw_dir = settings.raw_dir
    archive = _obtain_archive(spec, raw_dir)
    digest = sha256_file(archive)
    size = archive.stat().st_size
    extracted = _verify_and_extract(archive, raw_dir / spec.name)

    blob_url = None
    if blob_store is not None:
        blob_url = blob_store.upload_file(
            settings.container_raw, f"pursue/r01/{archive.name}", archive,
            overwrite=False)

    result = AcquiredBundle(
        name=spec.name, kind=spec.kind, archive_path=str(archive),
        extracted_dir=str(extracted), sha256=digest, size_bytes=size,
        blob_url=blob_url,
    )
    # Write a sidecar manifest for provenance/immutability audit.
    (raw_dir / f"{spec.name}.manifest.json").write_text(
        json.dumps(asdict(result), indent=2), encoding="utf-8")
    log.info("Acquired bundle '%s': %.1f MB sha256=%s", spec.name,
             size / 1e6, digest[:16])
    return result


def capture_portal_metadata(settings: Settings, export_path: Optional[str]) -> Optional[Path]:
    """Store the portal's Release-01 records database.

    Expected columns: file_name, agency, incident_date, location, type.
    If ``export_path`` is provided (a manual CSV export the operator downloaded
    from the portal), it is copied into the work dir. If not, we write a
    template and instruct the operator to fill it.
    """
    dest = settings.portal_metadata_path
    if export_path:
        src = Path(export_path).expanduser().resolve()
        if not src.exists():
            raise FileNotFoundError(f"Portal export not found: {src}")
        shutil.copy2(src, dest)
        log.info("Captured portal records DB -> %s", dest)
        return dest

    if dest.exists():
        log.info("Portal records DB already present: %s", dest)
        return dest

    # Write a template the operator can fill from the portal UI.
    template = "file_name,agency,incident_date,location,type\n"
    dest.write_text(template, encoding="utf-8")
    log.warning("No portal export supplied. Wrote template -> %s", dest)
    log.warning("Fill it with the Release-01 records DB (agency, incident date, "
                "location, type per file) and re-run, or pass --portal-export.")
    return dest


def run(settings: Settings, *, specs: List[BundleSpec], portal_export: Optional[str],
        blob_store=None) -> Dict[str, AcquiredBundle]:
    """Execute Step 2. Returns a mapping name -> AcquiredBundle."""
    settings.ensure_dirs()
    acquired: Dict[str, AcquiredBundle] = {}
    for spec in specs:
        acquired[spec.name] = acquire_bundle(spec, settings, blob_store=blob_store)
    capture_portal_metadata(settings, portal_export)

    # persist an acquisition summary
    summary = {k: asdict(v) for k, v in acquired.items()}
    (settings.state_dir / "acquisition.json").write_text(
        json.dumps(summary, indent=2), encoding="utf-8")
    return acquired
