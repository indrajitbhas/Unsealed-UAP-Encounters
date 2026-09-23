"""Thin wrappers around the Azure SDKs used by the probe.

All Azure access funnels through here so that:

* imports of ``azure-*`` packages are lazy (the pipeline can be inspected and
  dry-run without them installed),
* ``dry_run`` short-circuits every network call with a deterministic stub,
* authentication uses ``DefaultAzureCredential`` (works with ``az login`` on
  the operator's identity — the same identity granted "Storage Blob Data
  Contributor" in Step 1).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Blob storage
# --------------------------------------------------------------------------- #

class BlobStore:
    """Uploads/lists immutable blobs in the storage account containers."""

    def __init__(self, account: str, dry_run: bool = False):
        self.account = account
        self.dry_run = dry_run
        self._service = None

    @property
    def account_url(self) -> str:
        return f"https://{self.account}.blob.core.windows.net"

    def _client(self):
        if self._service is not None:
            return self._service
        # Lazy import so dry-run works without the package.
        from azure.identity import DefaultAzureCredential
        from azure.storage.blob import BlobServiceClient
        cred = DefaultAzureCredential()
        self._service = BlobServiceClient(self.account_url, credential=cred)
        return self._service

    def ensure_container(self, name: str) -> None:
        if self.dry_run:
            log.info("[dry-run] ensure container '%s'", name)
            return
        from azure.core.exceptions import ResourceExistsError
        svc = self._client()
        try:
            svc.create_container(name)
            log.info("Created container '%s'", name)
        except ResourceExistsError:
            log.debug("Container '%s' already exists", name)

    def upload_file(self, container: str, blob_name: str, path: Path,
                    *, overwrite: bool = False) -> str:
        """Upload a local file. Returns the blob URL. Immutable by default
        (overwrite=False) so raw acquisitions cannot be silently replaced."""
        if self.dry_run:
            log.info("[dry-run] upload %s -> %s/%s", path.name, container, blob_name)
            return f"{self.account_url}/{container}/{blob_name}"
        svc = self._client()
        bc = svc.get_blob_client(container=container, blob=blob_name)
        with path.open("rb") as fh:
            bc.upload_blob(fh, overwrite=overwrite)
        log.info("Uploaded %s -> %s/%s", path.name, container, blob_name)
        return bc.url

    def list_blobs(self, container: str, prefix: str = "") -> List[str]:
        if self.dry_run:
            return []
        svc = self._client()
        cc = svc.get_container_client(container)
        return [b.name for b in cc.list_blobs(name_starts_with=prefix)]


# --------------------------------------------------------------------------- #
# Document Intelligence (prebuilt-read OCR)
# --------------------------------------------------------------------------- #

class DocIntelClient:
    """Runs prebuilt-read OCR over specific page ranges of a PDF."""

    def __init__(self, endpoint: str, key: str, dry_run: bool = False):
        self.endpoint = endpoint
        self.key = key
        self.dry_run = dry_run
        self._client = None

    def _get(self):
        if self._client is not None:
            return self._client
        from azure.core.credentials import AzureKeyCredential
        from azure.ai.documentintelligence import DocumentIntelligenceClient
        self._client = DocumentIntelligenceClient(
            endpoint=self.endpoint,
            credential=AzureKeyCredential(self.key),
        )
        return self._client

    def analyze_pages(self, pdf_path: Path, pages: str) -> "OcrResult":
        """OCR a page range like ``"1-50"``.

        Returns an :class:`OcrResult`. In dry-run mode, returns deterministic
        synthetic text/confidence so the loop, ledger and checkpoints can be
        exercised without a live resource.
        """
        if self.dry_run:
            return _synthetic_ocr(pdf_path, pages)

        from azure.ai.documentintelligence.models import AnalyzeDocumentRequest
        client = self._get()
        with pdf_path.open("rb") as fh:
            poller = client.begin_analyze_document(
                "prebuilt-read",
                AnalyzeDocumentRequest(bytes_source=fh.read()),
                pages=pages,
            )
        result = poller.result()
        return OcrResult.from_di(result)


# --------------------------------------------------------------------------- #
# Result model shared by DI + Tesseract engines
# --------------------------------------------------------------------------- #

class OcrPage:
    def __init__(self, page_number: int, text: str, confidence: float):
        self.page_number = page_number
        self.text = text
        # mean confidence in 0..1; -1 means "engine did not report confidence"
        self.confidence = confidence

    @property
    def char_count(self) -> int:
        return len(self.text)


class OcrResult:
    def __init__(self, pages: List[OcrPage]):
        self.pages = pages

    @property
    def full_text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def mean_confidence(self) -> float:
        vals = [p.confidence for p in self.pages if p.confidence >= 0]
        return round(sum(vals) / len(vals), 4) if vals else -1.0

    @classmethod
    def from_di(cls, result) -> "OcrResult":
        """Adapt an azure-ai-documentintelligence AnalyzeResult.

        DI reports word-level confidence; we aggregate to a per-page mean and
        reconstruct page text from lines (falling back to words)."""
        pages: List[OcrPage] = []
        for page in getattr(result, "pages", []) or []:
            words = getattr(page, "words", None) or []
            confs = [w.confidence for w in words
                     if getattr(w, "confidence", None) is not None]
            mean_conf = round(sum(confs) / len(confs), 4) if confs else -1.0
            lines = getattr(page, "lines", None) or []
            if lines:
                text = "\n".join(getattr(ln, "content", "") for ln in lines)
            else:
                text = " ".join(getattr(w, "content", "") for w in words)
            pages.append(OcrPage(getattr(page, "page_number", len(pages) + 1),
                                 text, mean_conf))
        return cls(pages)


def _synthetic_ocr(pdf_path: Path, pages: str) -> OcrResult:
    """Deterministic fake OCR output for dry-run mode."""
    import hashlib
    start, _, end = pages.partition("-")
    lo = int(start)
    hi = int(end) if end else lo
    out: List[OcrPage] = []
    for pnum in range(lo, hi + 1):
        seed = hashlib.sha256(f"{pdf_path.name}:{pnum}".encode()).hexdigest()
        # confidence pseudo-derived from the hash so it's stable but varied.
        conf = 0.72 + (int(seed[:2], 16) / 255.0) * 0.27  # 0.72..0.99
        text = (
            f"[SYNTHETIC OCR — dry-run]\n"
            f"Document: {pdf_path.name}  Page: {pnum}\n"
            f"MEMORANDUM. Date: 1975-07-1{pnum % 10}. "
            f"Location: Wright-Patterson AFB, Ohio. "
            f"Agency: Department of Defense. Role: Intelligence Officer. "
            f"Ref {seed[:8]}."
        )
        out.append(OcrPage(pnum, text, round(conf, 4)))
    return OcrResult(out)
