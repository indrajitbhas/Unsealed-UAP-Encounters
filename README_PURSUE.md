# PURSUE R01 Extraction Probe

A Phase-0 validation workflow that processes **Release 01** of the PURSUE UAP
portal (<https://www.war.gov/ufo/>) to size a full six-tranche extraction
project while keeping Azure spend under a hard **$25 cap**.

It is built **on top of** this repo's Azure AI Foundry starter
(`run_agent.py`, `INSTRUCTIONS.md`) and orchestrated with the
**Microsoft Agent Framework** — the five steps are real `Executor` nodes joined
by edges, with a first-class **human-in-the-loop** checkpoint after every OCR
page bundle.

```
Step1Prereqs ─▶ Step2Acquire ─▶ Step3Inventory ─▶ Step4Extract ─▶ Step5Report
  (az script      (download/       (sha256, pages,   (bundled OCR +   (probe_report.md
   + HITL gate)    verify/store)    text-layer,       HITL after        + cost
                                    ffprobe)          EVERY bundle)     extrapolation)
```

## Why the Microsoft Agent Framework?

The orchestration layer (`pursue/workflow.py`) uses:

* **`WorkflowBuilder` + `Executor` subclasses + edges** — a real, inspectable
  workflow graph (the same primitive the framework uses for *agents in
  workflows*), one executor per step.
* **`WorkflowContext.request_info(...)` + `@response_handler`** — the idiomatic
  framework pattern for human-in-the-loop. Step 4 suspends the *entire workflow*
  after each page bundle, surfaces a quality sample + cost picture, and resumes
  only when the operator's decision is delivered via
  `workflow.run(responses={request_id: decision})`.
* **`agent_framework.foundry.FoundryChatClient`** (optional) — ties the probe to
  the same Azure AI Foundry project used by `run_agent.py`
  (`https://speedlearning.services.ai.azure.com/api/projects/proj-default`).
  See `maybe_build_foundry_narrator()`.

The business logic itself (OCR engines, ledgers, inventory, entity extraction,
report) lives in the `pursue/` step modules and is **wrapped**, not rewritten,
by the executors.

## Guardrails (hard requirements)

| Rule | Where enforced |
|---|---|
| **$25 spend cap** — halt + report before any bundle whose *projected* cost would exceed it | `pursue/state.py` `SpendTracker.check_projection`, checked in Step 4 before every bundle |
| **Human approval after EVERY bundle** — quality sample (confidence + excerpt), cost-to-date, projected total; operator can continue / resize N / switch engine / stop | `pursue/workflow.py` `Step4ExtractExecutor` via `request_info` + `pursue/checkpoint.py` |
| **Idempotent ledger** — never reprocess a logged page | `pursue/state.py` `PageLedger` (`processed_pages.jsonl`) |
| **Dynamic page bundles** (start N=50, operator-resizable) | Step 4 loop |
| **Engine switchable** DI ⇄ Tesseract at any checkpoint | `pursue/engines.py` `build_engine` |
| **Videos inventory-only** (count, sha256, ffprobe duration — never transcribed) | `pursue/step3_inventory.py` / Step 4 only touches `kind == documents` |
| **Neutral extraction only** (dates, places, agencies, roles — zero interpretation) | `pursue/extract_entities.py` |

## Usage

```bash
pip install -r requirements.txt

# Original Foundry 'hello agent' sample (unchanged):
python run_agent.py smoke

# Full probe — DRY RUN (no Azure; OCR/upload/provisioning simulated):
python run_agent.py probe \
    --documents ./documents.zip \
    --videos    ./videos.zip \
    --portal-export ./records.csv \
    --work-dir ./workdir \
    --dry-run

# Full probe — LIVE (attended; pauses at each checkpoint for your decision):
python run_agent.py probe \
    --documents "<direct-url-or-local-zip>" \
    --videos    "<direct-url-or-local-zip>" \
    --portal-export ./portal_records_r01.csv \
    --bundle-size 50 --spend-cap 25 --di-sku F0 --upload
```

At each Step-4 checkpoint you type one of:

```
c            continue with current settings
r 100        resize the bundle to 100 pages
e tesseract  switch OCR engine (documentintelligence | tesseract)
s            stop and write the report
```

> The PURSUE portal renders its download links with JavaScript, so direct
> bundle URLs or local zip paths must be supplied (Step 2). Provide the portal's
> R01 records DB as a CSV (`file_name,agency,incident_date,location,type`) via
> `--portal-export`.

## Step 1 — provisioning

Step 1 does **not** silently create cloud resources. It generates an idempotent
`az` script (`workdir/provision_azure.sh`) that creates the resource group, a
Storage account (containers `raw`/`processed`/`logs` + *Storage Blob Data
Contributor*), and a Document Intelligence resource (F0 free → S0). Run it in an
authenticated `az` session, export the printed env vars, then approve the
checkpoint to let the probe verify and continue.

## Outputs (in `--work-dir`)

* `provision_azure.sh` — Azure provisioning script (Step 1)
* `portal_records_r01.csv` — captured portal records DB (Step 2)
* `inventory.csv` — per-file inventory joined with portal metadata (Step 3)
* `processed/<file_id>.md` — page-marked OCR text (Step 4)
* `processed/<file_id>.json` — per-doc metadata (tranche, agency, incident
  date/location, page count, token count, OCR-quality flag, entities) (Step 4)
* `state/processed_pages.jsonl` — idempotent page ledger
* `state/spend_ledger.jsonl` — append-only spend ledger (cap source of truth)
* `probe_report.md` — final report: files/pages/tokens/media minutes, OCR
  failure rate, actual spend per service, whole-project (R01–R06) cost
  extrapolation, and an OCR-strategy recommendation (Step 5)

## Tests

`pursue`-level and workflow-level dry-run tests exercise the HITL pause/resume,
idempotency (a second run processes 0 pages), operator-stop, spend-cap
enforcement, and engine-switch/resize — all without any Azure resources.
```bash
python test_workflow.py   # (dry-run, from the test fixtures)
```

## Verified vs. not verified

* **Verified in this environment (dry-run / mock):** the full 5-step graph runs
  end-to-end through the Agent Framework; HITL `request_info`/`response_handler`
  pause-and-resume; idempotent ledger; spend-cap projection/halt; engine switch
  + bundle resize; report generation.
* **NOT exercised here (needs live Azure credentials):** real Azure Document
  Intelligence OCR, Blob uploads, `az` provisioning, and the Foundry narrator
  agent. The code paths exist and are gated behind live mode, but were not run
  against a provisioned subscription.
