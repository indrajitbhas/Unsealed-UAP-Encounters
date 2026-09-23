"""Central configuration, pricing constants and directory layout.

Everything that a human might reasonably want to tweak lives here so the rest
of the codebase can stay declarative. Values can be overridden with
environment variables (see :func:`load_settings`).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict


# --------------------------------------------------------------------------- #
# Hard limits & business rules
# --------------------------------------------------------------------------- #

#: Absolute Azure spend ceiling for this Phase-0 probe (USD). The runner halts
#: and writes the report before any step whose *projected* cumulative spend
#: would exceed this number.
SPEND_CAP_USD = 25.00

#: Default page-bundle size for the extraction loop. The human operator may
#: resize this at any checkpoint.
DEFAULT_BUNDLE_SIZE = 50

#: Below this mean OCR confidence a page/bundle is flagged low quality.
OCR_CONFIDENCE_FLOOR = 0.80

#: Rough characters-per-token ratio used for token estimation (GPT-style BPE).
CHARS_PER_TOKEN = 4.0


# --------------------------------------------------------------------------- #
# Azure pricing (USD) — realistic public list prices, used for estimation only.
# Actual invoiced cost may differ; these drive the spend projections/cap.
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Pricing:
    # Document Intelligence prebuilt-read (OCR) on the S0 tier: ~$1.50 / 1000 pages.
    di_read_per_page: float = 1.50 / 1000.0
    # F0 free tier: first 500 pages/month are free.
    di_free_pages_per_month: int = 500
    # Blob storage (hot LRS): ~$0.0184 per GB-month — negligible for this volume.
    storage_per_gb_month: float = 0.0184
    # Blob write operations: ~$0.05 per 10k writes — effectively zero here.
    storage_write_per_10k: float = 0.05
    # Tesseract runs locally on the agent VM: no Azure cost.
    tesseract_per_page: float = 0.0


PRICING = Pricing()


# --------------------------------------------------------------------------- #
# Project scope — the six tranches and their *document* bundle sizes (GB).
# Used for whole-project cost extrapolation in the final report.
# --------------------------------------------------------------------------- #

TRANCHE_DOC_SIZES_GB: Dict[str, float] = {
    "R01": 1.20,
    "R02": 0.07,
    "R03": 0.826,
    "R04": 0.227,
    "R05": 0.13,
    "R06": 2.23,
}


# --------------------------------------------------------------------------- #
# Directory layout
# --------------------------------------------------------------------------- #

@dataclass
class Settings:
    """Runtime settings resolved from environment + defaults."""

    # Root working directory for all generated artifacts.
    work_dir: Path = field(default_factory=lambda: Path(
        os.environ.get("PURSUE_WORK_DIR", "./workdir")).expanduser().resolve())

    # Azure resource identifiers (needed for Steps 1-4).
    subscription_id: str = os.environ.get("AZURE_SUBSCRIPTION_ID", "")
    resource_group: str = os.environ.get("PURSUE_RESOURCE_GROUP", "pursue-r01-rg")
    location: str = os.environ.get("PURSUE_LOCATION", "eastus")
    storage_account: str = os.environ.get("PURSUE_STORAGE_ACCOUNT", "pursuer01store")
    di_resource_name: str = os.environ.get("PURSUE_DI_RESOURCE", "pursue-r01-di")

    # Document Intelligence endpoint/key (populated after Step 1).
    di_endpoint: str = os.environ.get("AZURE_DI_ENDPOINT", "")
    di_key: str = os.environ.get("AZURE_DI_KEY", "")

    # Storage containers.
    container_raw: str = "raw"
    container_processed: str = "processed"
    container_logs: str = "logs"

    # Behaviour flags.
    spend_cap_usd: float = SPEND_CAP_USD
    bundle_size: int = DEFAULT_BUNDLE_SIZE
    #: When True, no Azure calls are made — OCR/upload are simulated. Lets the
    #: whole pipeline be exercised end-to-end without provisioned resources.
    dry_run: bool = os.environ.get("PURSUE_DRY_RUN", "0") == "1"
    #: When True, checkpoints auto-approve "continue" (for unattended demos/CI).
    auto_approve: bool = os.environ.get("PURSUE_AUTO_APPROVE", "0") == "1"

    # ------------------------------------------------------------------ #
    # Derived paths
    # ------------------------------------------------------------------ #
    @property
    def raw_dir(self) -> Path:
        return self.work_dir / "raw" / "pursue" / "r01"

    @property
    def processed_dir(self) -> Path:
        return self.work_dir / "processed"

    @property
    def logs_dir(self) -> Path:
        return self.work_dir / "logs"

    @property
    def state_dir(self) -> Path:
        return self.work_dir / "state"

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / "processed_pages.jsonl"

    @property
    def spend_ledger_path(self) -> Path:
        return self.state_dir / "spend_ledger.jsonl"

    @property
    def inventory_path(self) -> Path:
        return self.work_dir / "inventory.csv"

    @property
    def portal_metadata_path(self) -> Path:
        return self.work_dir / "portal_records_r01.csv"

    @property
    def report_path(self) -> Path:
        return self.work_dir / "probe_report.md"

    @property
    def provision_script_path(self) -> Path:
        return self.work_dir / "provision_azure.sh"

    def ensure_dirs(self) -> None:
        for p in (self.work_dir, self.raw_dir, self.processed_dir,
                  self.logs_dir, self.state_dir):
            p.mkdir(parents=True, exist_ok=True)

    def to_dict(self) -> dict:
        d = asdict(self)
        # dataclass asdict does not include properties; add key derived paths.
        d["work_dir"] = str(self.work_dir)
        # redact secrets
        if d.get("di_key"):
            d["di_key"] = "***redacted***"
        return d


def load_settings(**overrides) -> Settings:
    """Build a :class:`Settings`, applying any keyword overrides (from CLI)."""
    s = Settings()
    for k, v in overrides.items():
        if v is not None and hasattr(s, k):
            setattr(s, k, v)
    return s
