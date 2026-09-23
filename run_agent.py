# Before running the sample:
#    pip install -r requirements.txt
#
# This file keeps the original Azure AI Foundry connection sample intact (see
# `foundry_smoke_test`, run with `python run_agent.py smoke`) and *builds on top*
# of it to drive the full PURSUE R01 Extraction Probe, a five-step,
# human-in-the-loop workflow orchestrated with the Microsoft Agent Framework
# (`python run_agent.py probe ...`). See pursue/workflow.py for the graph.

import argparse
import logging
import os
import sys

# NOTE: the Azure AI Foundry SDK (azure-ai-projects / azure-identity) is imported
# lazily inside the Foundry helpers below so the PURSUE probe workflow can run
# (including --dry-run) even where those packages are absent or a different
# version is installed.

# The user's Azure AI Foundry project endpoint (resource "speedlearning",
# project "proj-default"). Override with FOUNDRY_PROJECT_ENDPOINT.
endpoint = os.environ.get(
    "FOUNDRY_PROJECT_ENDPOINT",
    "https://speedlearning.services.ai.azure.com/api/projects/proj-default",
)


def foundry_client():
    """Return an authenticated Foundry project client using the same
    conventions as the original sample (DefaultAzureCredential)."""
    from azure.identity import DefaultAzureCredential
    from azure.ai.projects import AIProjectClient
    return AIProjectClient(endpoint=endpoint, credential=DefaultAzureCredential())


def foundry_smoke_test() -> None:
    """The original Foundry 'hello agent' sample — unchanged in spirit.

    Streams a single turn against a Foundry workflow/agent and prints the
    workflow-action + text events. Requires an authenticated Azure session and
    an agent named below.
    """
    from azure.ai.projects.models import ResponseStreamEventType

    project_client = foundry_client()

    with project_client:

        workflow = {
            "name": "<your-agent-name>",
            "version": "<your-agent-version>",
        }

        openai_client = project_client.get_openai_client()

        conversation = openai_client.conversations.create()
        print(f"Created conversation (id: {conversation.id})")

        stream = openai_client.responses.create(
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": workflow["name"], "type": "agent_reference"}},
            input="Hello Agent",
            stream=True,
            metadata={"x-ms-debug-mode-enabled": "1"},
        )

        for event in stream:
            if event.type == ResponseStreamEventType.RESPONSE_OUTPUT_TEXT_DONE:
                print("\t", event.text)
            elif event.type == ResponseStreamEventType.RESPONSE_OUTPUT_ITEM_ADDED and event.item.type == "workflow_action":
                print(f"********************************\nActor - '{event.item.action_id}' :")
            elif event.type == ResponseStreamEventType.RESPONSE_OUTPUT_ITEM_ADDED and event.item.type == "workflow_action":
                print(f"Workflow Item '{event.item.action_id}' is '{event.item.status}' - (previous item was : '{event.item.previous_action_id}')")
            elif event.type == ResponseStreamEventType.RESPONSE_OUTPUT_ITEM_DONE and event.item.type == "workflow_action":
                print(f"Workflow Item '{event.item.action_id}' is '{event.item.status}' - (previous item was: '{event.item.previous_action_id}')")
            elif event.type == ResponseStreamEventType.RESPONSE_OUTPUT_TEXT_DELTA:
                print(event.delta)
            else:
                print(f"Unknown event: {event}")

        openai_client.conversations.delete(conversation_id=conversation.id)
        print("Conversation deleted")


# --------------------------------------------------------------------------- #
# PURSUE R01 Extraction Probe — Microsoft Agent Framework workflow runner
# --------------------------------------------------------------------------- #

def _run_probe(args: argparse.Namespace) -> int:
    """Drive the five-step probe workflow (pursue/workflow.py)."""
    from pursue.config import load_settings
    from pursue.logging_utils import setup_logging
    from pursue.step2_acquire import BundleSpec
    from pursue.workflow import run_probe

    from pathlib import Path
    overrides = {}
    if args.work_dir:
        overrides["work_dir"] = Path(args.work_dir).expanduser().resolve()
    if args.bundle_size:
        overrides["bundle_size"] = args.bundle_size
    if args.spend_cap is not None:
        overrides["spend_cap_usd"] = args.spend_cap
    overrides["dry_run"] = args.dry_run
    overrides["auto_approve"] = args.auto_approve
    settings = load_settings(**overrides)

    settings.ensure_dirs()
    setup_logging(settings.logs_dir,
                  level=logging.DEBUG if args.verbose else logging.INFO)
    log = logging.getLogger("pursue")

    # Build the two R01 bundles from the operator-supplied sources. The portal
    # renders download links with JavaScript, so direct URLs or local zip paths
    # must be provided (see pursue/step2_acquire.py).
    specs = []
    if args.documents:
        specs.append(BundleSpec(name="documents", source=args.documents, kind="documents"))
    if args.videos:
        specs.append(BundleSpec(name="videos", source=args.videos, kind="videos"))
    if not specs:
        log.error("Provide at least --documents <url|zip> (and optionally "
                  "--videos <url|zip>). The videos bundle is inventory-only.")
        return 2

    # Optional live Azure Blob store (skipped in dry-run).
    blob_store = None
    if not settings.dry_run and args.upload:
        from pursue.azure_clients import BlobStore
        blob_store = BlobStore(settings.storage_account, dry_run=False)

    log.info("Starting PURSUE R01 probe (dry_run=%s, auto_approve=%s, "
             "bundle_size=%d, cap=$%.2f)", settings.dry_run,
             settings.auto_approve, settings.bundle_size, settings.spend_cap_usd)

    report = run_probe(settings, specs=specs, portal_export=args.portal_export,
                       di_sku=args.di_sku, blob_store=blob_store)
    if report:
        log.info("Probe complete. Report: %s", report)
        print(f"\nReport written to: {report}")
        return 0
    log.error("Probe finished without producing a report.")
    return 1


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="run_agent.py",
        description="Azure AI Foundry sample + PURSUE R01 Extraction Probe "
                    "(Microsoft Agent Framework workflow).")
    sub = p.add_subparsers(dest="command")

    sub.add_parser("smoke", help="Run the original Foundry 'hello agent' sample.")

    probe = sub.add_parser(
        "probe", help="Run the five-step PURSUE R01 extraction probe workflow.")
    probe.add_argument("--documents", help="R01 documents bundle: URL or local .zip")
    probe.add_argument("--videos", help="R01 videos bundle: URL or local .zip "
                                        "(inventory only, never transcribed)")
    probe.add_argument("--portal-export", dest="portal_export",
                       help="CSV export of the portal's R01 records DB "
                            "(file_name,agency,incident_date,location,type)")
    probe.add_argument("--work-dir", dest="work_dir",
                       help="Working directory for all generated artifacts.")
    probe.add_argument("--bundle-size", dest="bundle_size", type=int,
                       help="Initial OCR page-bundle size (default 50).")
    probe.add_argument("--spend-cap", dest="spend_cap", type=float,
                       help="Hard Azure spend cap in USD (default 25.00).")
    probe.add_argument("--di-sku", dest="di_sku", default="F0",
                       choices=["F0", "S0"], help="Document Intelligence SKU.")
    probe.add_argument("--dry-run", action="store_true",
                       help="Simulate OCR/upload/provisioning (no Azure calls).")
    probe.add_argument("--auto-approve", action="store_true",
                       help="Skip interactive checkpoints (continue every bundle).")
    probe.add_argument("--upload", action="store_true",
                       help="Upload raw/processed artifacts to Blob storage "
                            "(ignored in --dry-run).")
    probe.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)
    if args.command == "probe":
        return _run_probe(args)
    # Default / "smoke": preserve original behaviour.
    foundry_smoke_test()
    return 0


if __name__ == "__main__":
    sys.exit(main())
