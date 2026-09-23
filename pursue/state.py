"""Persistent, idempotent state: the page ledger and the spend ledger.

Two append-only JSONL files back the whole pipeline's durability guarantees:

* ``processed_pages.jsonl`` — one record per (file_id, page) successfully
  OCR'd. Re-runs consult this ledger and never reprocess a logged page.
* ``spend_ledger.jsonl`` — one record per billable action. The cumulative sum
  is the source of truth for the $25 cap.

JSONL (append-only) is chosen deliberately: a crash mid-write loses at most the
final line, and recovery is a simple re-read. Both ledgers are safe to read on
every run and are the mechanism that makes the workflow resumable.
"""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, Optional, Set, Tuple

log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------------- #
# Page ledger
# --------------------------------------------------------------------------- #

@dataclass
class PageRecord:
    file_id: str
    page: int
    engine: str            # "documentintelligence" | "tesseract"
    confidence: float      # mean confidence 0..1 (None-safe -> -1 if unknown)
    char_count: int
    token_estimate: int
    bundle_id: str
    ts: str = ""

    def key(self) -> Tuple[str, int]:
        return (self.file_id, self.page)


class PageLedger:
    """Append-only ledger of processed pages, guaranteeing idempotency."""

    def __init__(self, path: Path):
        self.path = path
        self._seen: Set[Tuple[str, int]] = set()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        count = 0
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    self._seen.add((rec["file_id"], int(rec["page"])))
                    count += 1
                except (json.JSONDecodeError, KeyError) as exc:
                    log.warning("Skipping corrupt ledger line: %s", exc)
        log.info("Loaded %d previously-processed pages from ledger.", count)

    def is_processed(self, file_id: str, page: int) -> bool:
        return (file_id, int(page)) in self._seen

    def processed_pages_for(self, file_id: str) -> Set[int]:
        return {p for (f, p) in self._seen if f == file_id}

    def record(self, rec: PageRecord) -> None:
        with self._lock:
            if rec.key() in self._seen:
                return  # never double-log
            rec.ts = rec.ts or _utcnow()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(rec)) + "\n")
            self._seen.add(rec.key())

    def total_pages(self) -> int:
        return len(self._seen)

    def iter_records(self) -> Iterator[dict]:
        if not self.path.exists():
            return iter(())
        def _gen():
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        yield json.loads(line)
        return _gen()


# --------------------------------------------------------------------------- #
# Spend ledger + cap enforcement
# --------------------------------------------------------------------------- #

class SpendCapExceeded(Exception):
    """Raised when a projected action would breach the spend cap."""


@dataclass
class SpendRecord:
    service: str           # "documentintelligence" | "storage" | ...
    description: str
    amount_usd: float
    ts: str = ""


class SpendTracker:
    """Tracks cumulative Azure spend and enforces the hard cap.

    The cap is *projective*: before committing to a billable batch you call
    :meth:`check_projection` with the estimated cost. If current + projected
    would exceed the cap, it raises :class:`SpendCapExceeded` so the caller can
    halt cleanly and write the report.
    """

    def __init__(self, path: Path, cap_usd: float):
        self.path = path
        self.cap_usd = cap_usd
        self._by_service: Dict[str, float] = {}
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                svc = rec["service"]
                self._by_service[svc] = self._by_service.get(svc, 0.0) + float(rec["amount_usd"])
            except (json.JSONDecodeError, KeyError) as exc:
                log.warning("Skipping corrupt spend line: %s", exc)
        if self._by_service:
            log.info("Loaded prior spend: $%.4f total.", self.total())

    def total(self) -> float:
        return round(sum(self._by_service.values()), 6)

    def by_service(self) -> Dict[str, float]:
        return {k: round(v, 6) for k, v in self._by_service.items()}

    def remaining(self) -> float:
        return round(self.cap_usd - self.total(), 6)

    def projected_total(self, additional_usd: float) -> float:
        return round(self.total() + additional_usd, 6)

    def would_exceed(self, additional_usd: float) -> bool:
        return self.projected_total(additional_usd) > self.cap_usd + 1e-9

    def check_projection(self, additional_usd: float, context: str = "") -> None:
        """Raise if committing ``additional_usd`` would breach the cap."""
        if self.would_exceed(additional_usd):
            raise SpendCapExceeded(
                f"Projected spend ${self.projected_total(additional_usd):.4f} "
                f"would exceed cap ${self.cap_usd:.2f} "
                f"(current ${self.total():.4f}, +${additional_usd:.4f}) "
                f"[{context}]"
            )

    def record(self, service: str, amount_usd: float, description: str = "") -> None:
        with self._lock:
            rec = SpendRecord(service=service, description=description,
                              amount_usd=round(amount_usd, 6), ts=_utcnow())
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(rec)) + "\n")
            self._by_service[service] = self._by_service.get(service, 0.0) + rec.amount_usd
