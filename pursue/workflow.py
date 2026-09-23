"""Microsoft Agent Framework orchestration for the PURSUE R01 Extraction Probe.

This module is the *orchestration layer*. It does **not** re-implement any of the
business logic that already lives in ``pursue/step1_prereqs.py`` …
``pursue/step5_report.py`` — instead it wires those five steps together as a
Microsoft Agent Framework **workflow** (``agent_framework.WorkflowBuilder`` +
``Executor`` subclasses joined by edges):

    Step1Prereqs ──▶ Step2Acquire ──▶ Step3Inventory ──▶ Step4Extract ──▶ Step5Report

Why the Agent Framework (and not a plain function chain)?

* **Executors + edges** give us a real, inspectable workflow graph — the same
  primitive the framework uses for "agents in workflows".
* **Human-in-the-loop** is a first-class framework feature. Step 4 pauses after
  *every* page bundle by calling :meth:`WorkflowContext.request_info`; the run
  suspends, the operator's decision is delivered back via
  ``workflow.run(responses={request_id: decision})`` and dispatched to the
  ``@response_handler`` — exactly the pattern documented at
  https://learn.microsoft.com/en-gb/agent-framework/workflows/ .
* The same graph can later be exposed as a Foundry agent (``workflow.as_agent``)
  or narrated by a Foundry chat agent (see :func:`maybe_build_foundry_narrator`),
  tying the probe to the user's Azure AI Foundry project used in ``run_agent.py``.

The workflow is fully runnable in ``--dry-run`` with no Azure resources: OCR,
uploads and provisioning are simulated so the whole HITL / ledger / spend-cap
machinery can be exercised end-to-end.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from agent_framework import (
    Executor,
    WorkflowBuilder,
    WorkflowContext,
    handler,
    response_handler,
)

from .config import Settings, PRICING, OCR_CONFIDENCE_FLOOR
from .checkpoint import CheckpointDecision, render_bundle_summary
from .state import PageLedger, PageRecord, SpendTracker, SpendCapExceeded
from .engines import build_engine, ENGINE_DI, OcrEngine
from .extract_entities import extract_entities, Entities
from . import pdf_utils
from . import step1_prereqs, step2_acquire, step3_inventory, step5_report
from .step2_acquire import BundleSpec, AcquiredBundle
from .step3_inventory import InventoryRow
from .step4_extract import (
    DocOutput,
    ExtractionResult,
    MarkdownWriter,
    _make_di_client,
    _project_bundle_cost,
    _quality_flag,
    _resolve_pdf,
    _samples_from_result,
    _token_estimate,
    _upload_doc_outputs,
)

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Messages passed along the workflow edges
# --------------------------------------------------------------------------- #

@dataclass
class ProbeStart:
    """Initial input to the workflow (delivered to the start executor)."""
    specs: List[BundleSpec]
    portal_export: Optional[str] = None
    di_sku: str = "F0"


@dataclass
class Step1Done:
    start: ProbeStart
    status: dict


@dataclass
class Step2Done:
    start: ProbeStart
    acquired: Dict[str, AcquiredBundle]


@dataclass
class Step3Done:
    inventory: List[InventoryRow]


@dataclass
class Step4Done:
    inventory: List[InventoryRow]
    extraction: ExtractionResult


# --------------------------------------------------------------------------- #
# Human-in-the-loop request payloads (the "data" carried by request_info)
# --------------------------------------------------------------------------- #

@dataclass
class ProvisionApproval:
    """Step-1 gate: shown to the operator after the az script is generated."""
    script_path: str
    resource_group: str
    storage_account: str
    di_resource: str


@dataclass
class ProvisionDecision:
    """Operator's answer to a :class:`ProvisionApproval`."""
    proceed: bool = True


@dataclass
class BundleCheckpoint:
    """Step-4 per-bundle checkpoint payload: quality sample + cost picture.

    The operator answers with a :class:`~pursue.checkpoint.CheckpointDecision`
    (continue / resize / engine / stop).
    """
    summary: str                 # human-readable render_bundle_summary text
    bundle_id: str
    file_id: str
    file_name: str
    engine: str
    bundle_size: int
    pages_in_bundle: int
    mean_confidence: float
    low_conf_pages: int
    samples: List[dict]          # [{page, confidence, excerpt}, ...]
    cost_to_date: float
    bundle_cost: float
    projected_total: float
    spend_cap: float


# --------------------------------------------------------------------------- #
# Step 1 — Azure prerequisites
# --------------------------------------------------------------------------- #

class Step1PrereqsExecutor(Executor):
    """Generates the idempotent ``az`` provisioning script, waits for the
    operator (HITL gate via ``request_info``), then verifies the resources."""

    def __init__(self, settings: Settings, executor_id: str = "step1_prereqs"):
        super().__init__(id=executor_id)
        self._settings = settings
        self._pending_start: Optional[ProbeStart] = None

    @handler
    async def run(self, msg: ProbeStart, ctx: WorkflowContext[Step1Done]) -> None:
        s = self._settings
        s.ensure_dirs()
        step1_prereqs.print_requirements(s)
        script = step1_prereqs.generate_provision_script(s, di_sku=msg.di_sku)
        log.info("Step 1: provisioning script -> %s", script)
        self._pending_start = msg

        # Only unattended (auto-approve) runs skip the human gate. In dry-run we
        # still surface the generated script for review (verification is stubbed).
        if s.auto_approve:
            status = step1_prereqs.verify_resources(s)
            status["ok"] = step1_prereqs._status_ok(status)
            step1_prereqs._log_status(status)
            await ctx.send_message(Step1Done(start=msg, status=status))
            return

        await ctx.request_info(
            request_data=ProvisionApproval(
                script_path=str(script),
                resource_group=s.resource_group,
                storage_account=s.storage_account,
                di_resource=s.di_resource_name,
            ),
            response_type=ProvisionDecision,
        )

    @response_handler
    async def on_provision(
        self,
        original_request: ProvisionApproval,
        response: ProvisionDecision,
        ctx: WorkflowContext[Step1Done],
    ) -> None:
        s = self._settings
        assert self._pending_start is not None
        if not response.proceed:
            log.warning("Step 1 not approved by operator; verifying anyway to "
                        "report current state.")
        status = step1_prereqs.verify_resources(s)
        status["ok"] = step1_prereqs._status_ok(status)
        step1_prereqs._log_status(status)
        await ctx.send_message(Step1Done(start=self._pending_start, status=status))


# --------------------------------------------------------------------------- #
# Step 2 — Acquire bundles + portal records DB
# --------------------------------------------------------------------------- #

class Step2AcquireExecutor(Executor):
    def __init__(self, settings: Settings, blob_store=None,
                 executor_id: str = "step2_acquire"):
        super().__init__(id=executor_id)
        self._settings = settings
        self._blob_store = blob_store

    @handler
    async def run(self, msg: Step1Done, ctx: WorkflowContext[Step2Done]) -> None:
        if not msg.status.get("ok", False):
            log.warning("Step 1 verification incomplete (%s); Step 2 will still "
                        "acquire from operator-supplied sources.", msg.status)
        acquired = step2_acquire.run(
            self._settings,
            specs=msg.start.specs,
            portal_export=msg.start.portal_export,
            blob_store=self._blob_store,
        )
        await ctx.send_message(Step2Done(start=msg.start, acquired=acquired))


# --------------------------------------------------------------------------- #
# Step 3 — Inventory
# --------------------------------------------------------------------------- #

class Step3InventoryExecutor(Executor):
    def __init__(self, settings: Settings, executor_id: str = "step3_inventory"):
        super().__init__(id=executor_id)
        self._settings = settings

    @handler
    async def run(self, msg: Step2Done, ctx: WorkflowContext[Step3Done]) -> None:
        inventory = step3_inventory.run(self._settings, msg.acquired)
        await ctx.send_message(Step3Done(inventory=inventory))


# --------------------------------------------------------------------------- #
# Step 4 — Bundled OCR with a human checkpoint after EVERY bundle
# --------------------------------------------------------------------------- #

@dataclass
class _DocAcc:
    """Per-document accumulator carried across request/response cycles."""
    inv: InventoryRow
    total_pages: int
    abs_pdf: Path
    md_path: Path
    writer: MarkdownWriter
    entities: Entities = field(default_factory=Entities)
    tokens: int = 0
    conf_sum: float = 0.0
    conf_count: int = 0
    low_pages: int = 0
    engine_last: str = ""


class Step4ExtractExecutor(Executor):
    """Resumable extraction loop expressed as a single Agent Framework executor.

    The loop body processes exactly one page bundle, then *suspends the whole
    workflow* with :meth:`WorkflowContext.request_info`, surfacing the quality
    sample + cost picture. The operator's :class:`CheckpointDecision` is routed
    back to :meth:`on_decision`, which applies continue / resize / engine / stop
    and drives the next bundle — the idiomatic Agent Framework HITL pattern.

    Idempotency and the spend cap come *for free* from the existing on-disk
    ledgers (``processed_pages.jsonl`` / ``spend_ledger.jsonl``): a re-run
    resumes exactly where it stopped and never reprocesses a logged page.
    """

    def __init__(self, settings: Settings, blob_store=None,
                 executor_id: str = "step4_extract"):
        super().__init__(id=executor_id)
        self._settings = settings
        self._blob_store = blob_store

    # -- state initialised at run start, mutated across resume cycles -------- #
    @handler
    async def run(self, msg: Step3Done, ctx: WorkflowContext[Step4Done]) -> None:
        s = self._settings
        s.ensure_dirs()
        self._ledger = PageLedger(s.ledger_path)
        self._spend = SpendTracker(s.spend_ledger_path, s.spend_cap_usd)
        self._di_client = _make_di_client(s)
        self._engine: OcrEngine = build_engine(
            ENGINE_DI, di_client=self._di_client,
            per_page_cost=PRICING.di_read_per_page, dry_run=s.dry_run)
        self._bundle_size = s.bundle_size
        self._inventory = msg.inventory

        docs = [r for r in msg.inventory
                if r.kind == "documents" and r.ext == ".pdf"]
        docs.sort(key=lambda r: (r.has_text_layer is True, r.file_name))
        self._docs = docs
        self._doc_idx = 0
        self._outputs: List[DocOutput] = []
        self._pages_before = self._ledger.total_pages()
        self._acc: Optional[_DocAcc] = None
        self._finalized: set = set()
        self._stopped_reason = "completed"

        await self._advance(ctx)

    # -- core loop: process the next bundle, then suspend on a checkpoint ---- #
    async def _advance(self, ctx: WorkflowContext[Step4Done]) -> None:
        s = self._settings
        while self._doc_idx < len(self._docs):
            inv = self._docs[self._doc_idx]

            if self._acc is None or self._acc.inv.file_id != inv.file_id:
                abs_pdf = _resolve_pdf(inv, s)
                if abs_pdf is None or not abs_pdf.exists():
                    log.error("Cannot locate PDF for %s (%s); skipping.",
                              inv.file_name, inv.file_id)
                    self._doc_idx += 1
                    self._acc = None
                    continue
                total_pages = inv.page_count or pdf_utils.page_count(abs_pdf)
                md_path = s.processed_dir / f"{inv.file_id}.md"
                self._acc = _DocAcc(
                    inv=inv, total_pages=total_pages, abs_pdf=abs_pdf,
                    md_path=md_path, writer=MarkdownWriter(md_path, inv.file_name),
                    engine_last=self._engine.name)

            acc = self._acc
            bundles = pdf_utils.page_ranges(
                acc.total_pages,
                self._ledger.processed_pages_for(inv.file_id),
                self._bundle_size)
            if not bundles:
                self._finalize_doc(acc)
                self._doc_idx += 1
                self._acc = None
                continue

            bundle_pages = bundles[0]
            bundle_id = f"{inv.file_id}:{bundle_pages[0]}-{bundle_pages[-1]}"

            # --- spend cap check BEFORE spending ---
            projected = _project_bundle_cost(
                self._engine, len(bundle_pages), self._spend, s)
            try:
                self._spend.check_projection(projected, context=f"bundle {bundle_id}")
            except SpendCapExceeded as exc:
                log.warning("SPEND CAP: %s", exc)
                self._stopped_reason = "spend_cap"
                await self._finish(ctx)
                return

            # --- OCR the bundle ---
            try:
                result = self._engine.ocr_pages(acc.abs_pdf, bundle_pages)
            except Exception as exc:  # noqa: BLE001
                log.error("OCR failed for %s pages %s: %s",
                          inv.file_name, bundle_id, exc)
                self._stopped_reason = "operator_stop"
                await self._finish(ctx)
                return

            if projected > 0:
                self._spend.record("documentintelligence", projected,
                                   f"OCR {len(bundle_pages)}p {bundle_id}")

            # --- persist pages + ledger + markdown + entities ---
            bundle_low = 0
            bundle_conf_vals: List[float] = []
            for pg in result.pages:
                if self._ledger.is_processed(inv.file_id, pg.page_number):
                    continue
                acc.writer.append_page(pg.page_number, pg.text, pg.confidence)
                acc.entities = acc.entities.merge(extract_entities(pg.text))
                tk = _token_estimate(pg.text)
                acc.tokens += tk
                if pg.confidence >= 0:
                    acc.conf_sum += pg.confidence
                    acc.conf_count += 1
                    bundle_conf_vals.append(pg.confidence)
                    if pg.confidence < OCR_CONFIDENCE_FLOOR:
                        acc.low_pages += 1
                        bundle_low += 1
                self._ledger.record(PageRecord(
                    file_id=inv.file_id, page=pg.page_number,
                    engine=self._engine.name, confidence=pg.confidence,
                    char_count=pg.char_count, token_estimate=tk,
                    bundle_id=bundle_id))
            acc.engine_last = self._engine.name

            # --- build + emit the human checkpoint (SUSPENDS the workflow) ---
            bundle_mean = (round(sum(bundle_conf_vals) / len(bundle_conf_vals), 4)
                           if bundle_conf_vals else -1.0)
            samples = _samples_from_result(result)
            summary = render_bundle_summary(
                bundle_id=bundle_id, file_id=inv.file_id, engine=self._engine.name,
                pages_in_bundle=len(result.pages), samples=samples,
                mean_confidence=bundle_mean, low_conf_pages=bundle_low,
                cost_to_date=self._spend.total(), bundle_cost=projected,
                projected_total=self._spend.projected_total(
                    _project_bundle_cost(self._engine, self._bundle_size,
                                         self._spend, s)),
                spend_cap=s.spend_cap_usd)
            log.info("Checkpoint after bundle %s (mean_conf=%.3f, low=%d)",
                     bundle_id, bundle_mean, bundle_low)

            await ctx.request_info(
                request_data=BundleCheckpoint(
                    summary=summary, bundle_id=bundle_id, file_id=inv.file_id,
                    file_name=inv.file_name, engine=self._engine.name,
                    bundle_size=self._bundle_size, pages_in_bundle=len(result.pages),
                    mean_confidence=bundle_mean, low_conf_pages=bundle_low,
                    samples=[{"page": s_.page, "confidence": s_.confidence,
                              "excerpt": " ".join(s_.excerpt.split())[:400]}
                             for s_ in samples],
                    cost_to_date=self._spend.total(), bundle_cost=projected,
                    projected_total=self._spend.projected_total(
                        _project_bundle_cost(self._engine, self._bundle_size,
                                             self._spend, s)),
                    spend_cap=s.spend_cap_usd),
                response_type=CheckpointDecision,
            )
            return  # suspend; resumes in on_decision

        # no more documents
        await self._finish(ctx)

    @response_handler
    async def on_decision(
        self,
        original_request: BundleCheckpoint,
        response: CheckpointDecision,
        ctx: WorkflowContext[Step4Done],
    ) -> None:
        s = self._settings
        if response.action == "stop":
            self._stopped_reason = "operator_stop"
            await self._finish(ctx)
            return
        if response.action == "resize" and response.bundle_size:
            self._bundle_size = response.bundle_size
            log.info("Operator resized bundle -> %d pages", self._bundle_size)
        elif response.action == "engine" and response.engine:
            self._engine = build_engine(
                response.engine, di_client=self._di_client,
                per_page_cost=PRICING.di_read_per_page, dry_run=s.dry_run)
            log.info("Operator switched engine -> %s", self._engine.name)
        # continue / resize / engine all fall through to the next bundle
        await self._advance(ctx)

    # -- finalisation ------------------------------------------------------- #
    def _ledger_stats_for(self, file_id: str) -> dict:
        """Cumulative per-document stats from the durable ledger, so resumed /
        idempotent runs report consistent totals instead of only this run's."""
        tokens = 0
        conf_sum = 0.0
        conf_count = 0
        low = 0
        for rec in self._ledger.iter_records():
            if rec.get("file_id") != file_id:
                continue
            tokens += int(rec.get("token_estimate", 0) or 0)
            c = rec.get("confidence", -1)
            if c is not None and c >= 0:
                conf_sum += c
                conf_count += 1
                if c < OCR_CONFIDENCE_FLOOR:
                    low += 1
        mean_conf = round(conf_sum / conf_count, 4) if conf_count else -1.0
        return {"tokens": tokens, "mean_conf": mean_conf,
                "conf_count": conf_count, "low": low}

    def _finalize_doc(self, acc: _DocAcc) -> None:
        from datetime import datetime, timezone
        s = self._settings
        inv = acc.inv
        if inv.file_id in self._finalized:
            return
        stats = self._ledger_stats_for(inv.file_id)
        pages_done = len(self._ledger.processed_pages_for(inv.file_id))

        # Entities are not in the ledger; merge this run's with any already
        # persisted in a prior run so cumulative extraction survives resumes.
        entities = acc.entities.to_dict()
        json_path = s.processed_dir / f"{inv.file_id}.json"
        if json_path.exists():
            try:
                prev = json.loads(json_path.read_text(encoding="utf-8"))
                prev_ent = prev.get("entities", {})
                merged = {k: sorted(set(entities.get(k, [])) | set(prev_ent.get(k, [])))
                          for k in set(entities) | set(prev_ent)}
                entities = merged
            except Exception:  # noqa: BLE001
                pass

        doc = DocOutput(
            file_id=inv.file_id, tranche="R01", file_name=inv.file_name,
            agency=inv.agency, incident_date=inv.incident_date,
            location=inv.location, page_count=acc.total_pages,
            pages_processed=pages_done, token_count=stats["tokens"],
            mean_confidence=stats["mean_conf"],
            ocr_quality_flag=_quality_flag(stats["mean_conf"], stats["low"],
                                           max(1, stats["conf_count"])),
            engine_last=acc.engine_last or self._engine.name, entities=entities,
            md_path=str(acc.md_path),
            generated_at=datetime.now(timezone.utc).isoformat())
        json_path.write_text(json.dumps(_dataclass_to_dict(doc), indent=2),
                             encoding="utf-8")
        self._outputs.append(doc)
        self._finalized.add(inv.file_id)
        if self._blob_store is not None:
            _upload_doc_outputs(self._blob_store, s, doc)

    async def _finish(self, ctx: WorkflowContext[Step4Done]) -> None:
        s = self._settings
        # finalise a document that was mid-flight when we stopped
        if self._acc is not None and self._acc.inv.file_id not in self._finalized:
            self._finalize_doc(self._acc)
        pages_after = self._ledger.total_pages()
        result = ExtractionResult(
            documents=self._outputs,
            pages_processed_this_run=pages_after - self._pages_before,
            stopped_reason=self._stopped_reason,
            engine_final=self._engine.name,
            bundle_size_final=self._bundle_size)
        (s.state_dir / "extraction_result.json").write_text(
            json.dumps({
                "pages_processed_this_run": result.pages_processed_this_run,
                "stopped_reason": result.stopped_reason,
                "engine_final": result.engine_final,
                "bundle_size_final": result.bundle_size_final,
                "documents": [_dataclass_to_dict(d) for d in result.documents],
            }, indent=2), encoding="utf-8")
        await ctx.send_message(Step4Done(inventory=self._inventory,
                                         extraction=result))


# --------------------------------------------------------------------------- #
# Step 5 — Report
# --------------------------------------------------------------------------- #

class Step5ReportExecutor(Executor):
    def __init__(self, settings: Settings, executor_id: str = "step5_report"):
        super().__init__(id=executor_id)
        self._settings = settings

    @handler
    async def run(self, msg: Step4Done, ctx: WorkflowContext[str, str]) -> None:
        report = step5_report.run(self._settings, inventory=msg.inventory,
                                  extraction=msg.extraction)
        log.info("Step 5: wrote report -> %s", report)
        await ctx.yield_output(str(report))


# --------------------------------------------------------------------------- #
# Small helper
# --------------------------------------------------------------------------- #

def _dataclass_to_dict(obj) -> dict:
    from dataclasses import asdict
    return asdict(obj)


# --------------------------------------------------------------------------- #
# Workflow assembly
# --------------------------------------------------------------------------- #

def build_workflow(settings: Settings, *, blob_store=None):
    """Assemble the five-step probe as an Agent Framework workflow graph."""
    step1 = Step1PrereqsExecutor(settings)
    step2 = Step2AcquireExecutor(settings, blob_store=blob_store)
    step3 = Step3InventoryExecutor(settings)
    step4 = Step4ExtractExecutor(settings, blob_store=blob_store)
    step5 = Step5ReportExecutor(settings)

    return (
        WorkflowBuilder(start_executor=step1, name="pursue-r01-extraction-probe")
        .add_edge(step1, step2)
        .add_edge(step2, step3)
        .add_edge(step3, step4)
        .add_edge(step4, step5)
        .build()
    )


# --------------------------------------------------------------------------- #
# Decision providers (how the human answers request_info during a run)
# --------------------------------------------------------------------------- #

# A decision provider maps the request_data object -> a response value.
DecisionProvider = Callable[[object], object]


def interactive_decision_provider(request_data: object) -> object:
    """Prompt the real operator on stdin (used by an attended CLI run)."""
    from .checkpoint import prompt_decision, confirm
    if isinstance(request_data, BundleCheckpoint):
        print(request_data.summary)
        return prompt_decision(
            current_bundle_size=request_data.bundle_size,
            current_engine=request_data.engine,
            auto_approve=False)
    if isinstance(request_data, ProvisionApproval):
        print("\nStep 1 — provisioning script generated at:")
        print(f"    {request_data.script_path}")
        print("Run it in an authenticated `az` session, then answer:")
        ok = confirm("Have you run the script and exported the env vars?",
                     default=False)
        return ProvisionDecision(proceed=ok)
    raise TypeError(f"No interactive handler for {type(request_data).__name__}")


def auto_continue_provider(request_data: object) -> object:
    """Unattended provider: approve provisioning, continue every bundle."""
    if isinstance(request_data, BundleCheckpoint):
        return CheckpointDecision(action="continue")
    if isinstance(request_data, ProvisionApproval):
        return ProvisionDecision(proceed=True)
    raise TypeError(f"No auto handler for {type(request_data).__name__}")


# --------------------------------------------------------------------------- #
# Async driver: run the workflow, servicing request_info pauses via a provider
# --------------------------------------------------------------------------- #

async def run_probe_async(
    settings: Settings,
    *,
    specs: List[BundleSpec],
    portal_export: Optional[str] = None,
    di_sku: str = "F0",
    blob_store=None,
    decision_provider: Optional[DecisionProvider] = None,
) -> Optional[str]:
    """Drive the probe workflow to completion.

    Streams events; whenever the workflow emits a ``request_info`` event it
    consults ``decision_provider`` for the response, then resumes the run with
    ``workflow.run(responses={request_id: value})`` — looping until the graph
    yields its final output (the report path).
    """
    if decision_provider is None:
        decision_provider = (auto_continue_provider
                             if settings.auto_approve or settings.dry_run
                             else interactive_decision_provider)

    workflow = build_workflow(settings, blob_store=blob_store)
    report_path: Optional[str] = None

    # First leg: start with the initial message.
    pending: Dict[str, object] = {}
    stream = workflow.run(ProbeStart(specs=specs, portal_export=portal_export,
                                     di_sku=di_sku), stream=True)

    while True:
        async for event in stream:
            etype = getattr(event, "type", None)
            if etype == "request_info":
                pending[event.request_id] = decision_provider(event.data)
            elif etype == "output":
                report_path = str(getattr(event, "data", "") or report_path)
            elif etype == "failed":
                log.error("Workflow failed: %s", getattr(event, "data", ""))

        if not pending:
            break
        responses, pending = pending, {}
        stream = workflow.run(responses=responses, stream=True)

    return report_path


def run_probe(
    settings: Settings,
    *,
    specs: List[BundleSpec],
    portal_export: Optional[str] = None,
    di_sku: str = "F0",
    blob_store=None,
    decision_provider: Optional[DecisionProvider] = None,
) -> Optional[str]:
    """Blocking wrapper around :func:`run_probe_async`."""
    import asyncio
    return asyncio.run(run_probe_async(
        settings, specs=specs, portal_export=portal_export, di_sku=di_sku,
        blob_store=blob_store, decision_provider=decision_provider))


# --------------------------------------------------------------------------- #
# Optional: a Foundry-backed narrator agent (ties probe to the user's Azure AI
# Foundry project — the same endpoint used by run_agent.py). No-op unless the
# Agent Framework Foundry extra is installed AND the endpoint env is configured.
# --------------------------------------------------------------------------- #

def maybe_build_foundry_narrator(instructions: str):
    """Return an Agent Framework agent backed by the user's Foundry project, or
    ``None`` if the Foundry client / credentials are unavailable.

    Endpoint resolution mirrors ``run_agent.py``:
    ``https://speedlearning.services.ai.azure.com/api/projects/proj-default``
    (overridable via ``FOUNDRY_PROJECT_ENDPOINT`` / ``FOUNDRY_MODEL``).
    """
    endpoint = os.environ.get(
        "FOUNDRY_PROJECT_ENDPOINT",
        "https://speedlearning.services.ai.azure.com/api/projects/proj-default")
    model = os.environ.get("FOUNDRY_MODEL", "")
    if not model:
        log.info("FOUNDRY_MODEL not set; skipping Foundry narrator agent.")
        return None
    try:
        from agent_framework.foundry import FoundryChatClient  # type: ignore
        from azure.identity import AzureCliCredential  # type: ignore
    except Exception as exc:  # noqa: BLE001
        log.info("Foundry client unavailable (%s); skipping narrator.", exc)
        return None
    try:
        client = FoundryChatClient(project_endpoint=endpoint, model=model,
                                   credential=AzureCliCredential())
        return client.as_agent(name="pursue-narrator", instructions=instructions)
    except Exception as exc:  # noqa: BLE001
        log.info("Could not build Foundry narrator (%s); continuing without.", exc)
        return None
