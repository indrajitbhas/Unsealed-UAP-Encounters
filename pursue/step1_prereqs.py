"""Step 1 — Check / provision Azure prerequisites.

We do NOT silently create cloud resources on the user's subscription. Instead we:

1. Tell the operator exactly what is needed.
2. Generate an idempotent ``az`` CLI provisioning script.
3. Wait for the operator to run it (checkpoint).
4. Verify the resources actually exist and are reachable.

Expected resources
------------------
* Resource group
* Storage account with containers raw / processed / logs, and the operator's
  identity granted "Storage Blob Data Contributor".
* Document Intelligence resource (start F0 free tier; upgrade to S0 when the
  free 500 pages/month are exhausted).
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Tuple

from .config import Settings
from .checkpoint import confirm

log = logging.getLogger(__name__)


PROVISION_TEMPLATE = """\
#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# PURSUE R01 Extraction Probe — Azure provisioning
# Idempotent: safe to re-run. Requires: az CLI (>=2.50), an authenticated
# session (`az login`), and an active subscription.
# ---------------------------------------------------------------------------
set -euo pipefail

SUBSCRIPTION_ID="{subscription_id}"
RESOURCE_GROUP="{resource_group}"
LOCATION="{location}"
STORAGE_ACCOUNT="{storage_account}"
DI_RESOURCE="{di_resource_name}"
DI_SKU="{di_sku}"   # F0 (free) or S0 (standard)

echo ">> Selecting subscription"
if [ -n "${{SUBSCRIPTION_ID}}" ]; then
  az account set --subscription "${{SUBSCRIPTION_ID}}"
fi
SUBSCRIPTION_ID="$(az account show --query id -o tsv)"
echo "   Using subscription: ${{SUBSCRIPTION_ID}}"

echo ">> Resource group: ${{RESOURCE_GROUP}}"
az group create --name "${{RESOURCE_GROUP}}" --location "${{LOCATION}}" -o none

echo ">> Storage account: ${{STORAGE_ACCOUNT}}"
if ! az storage account show -n "${{STORAGE_ACCOUNT}}" -g "${{RESOURCE_GROUP}}" -o none 2>/dev/null; then
  az storage account create \\
    --name "${{STORAGE_ACCOUNT}}" \\
    --resource-group "${{RESOURCE_GROUP}}" \\
    --location "${{LOCATION}}" \\
    --sku Standard_LRS \\
    --kind StorageV2 \\
    --min-tls-version TLS1_2 \\
    --allow-blob-public-access false \\
    -o none
fi

echo ">> Granting 'Storage Blob Data Contributor' to your identity"
CALLER_OID="$(az ad signed-in-user show --query id -o tsv)"
STORAGE_ID="$(az storage account show -n "${{STORAGE_ACCOUNT}}" -g "${{RESOURCE_GROUP}}" --query id -o tsv)"
az role assignment create \\
  --assignee-object-id "${{CALLER_OID}}" \\
  --assignee-principal-type User \\
  --role "Storage Blob Data Contributor" \\
  --scope "${{STORAGE_ID}}" -o none || echo "   (role assignment may already exist)"

echo ">> Containers: raw / processed / logs"
for c in {container_raw} {container_processed} {container_logs}; do
  az storage container create \\
    --name "${{c}}" \\
    --account-name "${{STORAGE_ACCOUNT}}" \\
    --auth-mode login \\
    -o none || true
done

echo ">> Document Intelligence: ${{DI_RESOURCE}} (SKU ${{DI_SKU}})"
if ! az cognitiveservices account show -n "${{DI_RESOURCE}}" -g "${{RESOURCE_GROUP}}" -o none 2>/dev/null; then
  az cognitiveservices account create \\
    --name "${{DI_RESOURCE}}" \\
    --resource-group "${{RESOURCE_GROUP}}" \\
    --location "${{LOCATION}}" \\
    --kind FormRecognizer \\
    --sku "${{DI_SKU}}" \\
    --yes -o none
fi

DI_ENDPOINT="$(az cognitiveservices account show -n "${{DI_RESOURCE}}" -g "${{RESOURCE_GROUP}}" --query properties.endpoint -o tsv)"
DI_KEY="$(az cognitiveservices account keys list -n "${{DI_RESOURCE}}" -g "${{RESOURCE_GROUP}}" --query key1 -o tsv)"

echo ""
echo "==========================================================================="
echo " PROVISIONING COMPLETE. Export these before running the probe:"
echo "==========================================================================="
echo "export AZURE_SUBSCRIPTION_ID=\\"${{SUBSCRIPTION_ID}}\\""
echo "export AZURE_DI_ENDPOINT=\\"${{DI_ENDPOINT}}\\""
echo "export AZURE_DI_KEY=\\"${{DI_KEY}}\\""
echo "export PURSUE_STORAGE_ACCOUNT=\\"${{STORAGE_ACCOUNT}}\\""
echo "export PURSUE_RESOURCE_GROUP=\\"${{RESOURCE_GROUP}}\\""
echo "==========================================================================="
"""


def _az_available() -> bool:
    return shutil.which("az") is not None


def generate_provision_script(settings: Settings, di_sku: str = "F0") -> Path:
    """Write the ``az`` provisioning script to the work dir and return its path."""
    script = PROVISION_TEMPLATE.format(
        subscription_id=settings.subscription_id,
        resource_group=settings.resource_group,
        location=settings.location,
        storage_account=settings.storage_account,
        di_resource_name=settings.di_resource_name,
        di_sku=di_sku,
        container_raw=settings.container_raw,
        container_processed=settings.container_processed,
        container_logs=settings.container_logs,
    )
    path = settings.provision_script_path
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    log.info("Wrote provisioning script -> %s", path)
    return path


def _run_az_json(args: list) -> Tuple[bool, dict]:
    try:
        out = subprocess.run(["az", *args], capture_output=True, text=True, timeout=120)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        log.error("az call failed: %s", exc)
        return False, {}
    if out.returncode != 0:
        log.debug("az %s -> rc=%d stderr=%s", " ".join(args), out.returncode,
                  out.stderr.strip())
        return False, {}
    try:
        return True, json.loads(out.stdout) if out.stdout.strip() else {}
    except json.JSONDecodeError:
        return True, {}


def verify_resources(settings: Settings) -> dict:
    """Verify the expected resources exist. Returns a status dict."""
    status = {
        "az_cli": _az_available(),
        "resource_group": False,
        "storage_account": False,
        "containers": {c: False for c in
                       (settings.container_raw, settings.container_processed,
                        settings.container_logs)},
        "document_intelligence": False,
        "di_endpoint_env": bool(settings.di_endpoint),
    }

    if settings.dry_run:
        log.info("[dry-run] Skipping live Azure verification; assuming OK.")
        status.update({
            "resource_group": True, "storage_account": True,
            "document_intelligence": True,
            "containers": {c: True for c in status["containers"]},
        })
        return status

    if not status["az_cli"]:
        log.error("az CLI not found; cannot verify resources.")
        return status

    ok, _ = _run_az_json(["group", "show", "-n", settings.resource_group])
    status["resource_group"] = ok

    ok, _ = _run_az_json(["storage", "account", "show", "-n",
                          settings.storage_account, "-g", settings.resource_group])
    status["storage_account"] = ok

    if ok:
        for c in list(status["containers"]):
            cok, _ = _run_az_json([
                "storage", "container", "show", "--name", c,
                "--account-name", settings.storage_account, "--auth-mode", "login",
            ])
            status["containers"][c] = cok

    ok, _ = _run_az_json(["cognitiveservices", "account", "show", "-n",
                          settings.di_resource_name, "-g", settings.resource_group])
    status["document_intelligence"] = ok

    return status


def print_requirements(settings: Settings) -> None:
    log.info("")
    log.info("STEP 1 — Azure prerequisites required for this probe:")
    log.info("  1. Resource group           : %s (%s)", settings.resource_group,
             settings.location)
    log.info("  2. Storage account          : %s (Standard_LRS, StorageV2)",
             settings.storage_account)
    log.info("     - containers            : %s, %s, %s", settings.container_raw,
             settings.container_processed, settings.container_logs)
    log.info("     - RBAC                   : 'Storage Blob Data Contributor' -> your identity")
    log.info("  3. Document Intelligence    : %s (start F0 free, S0 when needed)",
             settings.di_resource_name)
    log.info("")


def run(settings: Settings, di_sku: str = "F0") -> dict:
    """Execute Step 1. Returns the final verification status dict."""
    print_requirements(settings)
    script_path = generate_provision_script(settings, di_sku=di_sku)

    log.info("A provisioning script has been generated:")
    log.info("    %s", script_path)
    log.info("Review it, then run it in an authenticated `az` session:")
    log.info("    bash %s", script_path)
    log.info("After it prints the export lines, set those env vars and re-run "
             "this step (or continue).")

    if not settings.dry_run:
        proceed = confirm(
            "Have you run the provisioning script and set the env vars?",
            auto_approve=settings.auto_approve, default=False)
        if not proceed:
            log.warning("Step 1 paused: run the script, then re-run `step1`.")
            return {"paused": True}

    status = verify_resources(settings)
    _log_status(status)
    status["ok"] = _status_ok(status)
    return status


def _status_ok(status: dict) -> bool:
    return (status.get("resource_group") and status.get("storage_account")
            and status.get("document_intelligence")
            and all(status.get("containers", {}).values()))


def _log_status(status: dict) -> None:
    log.info("Verification:")
    log.info("  az CLI                 : %s", "OK" if status["az_cli"] else "MISSING")
    log.info("  resource group         : %s", "OK" if status["resource_group"] else "MISSING")
    log.info("  storage account        : %s", "OK" if status["storage_account"] else "MISSING")
    for c, v in status["containers"].items():
        log.info("  container '%s'%s: %s", c, " " * max(0, 10 - len(c)),
                 "OK" if v else "MISSING")
    log.info("  document intelligence  : %s",
             "OK" if status["document_intelligence"] else "MISSING")
