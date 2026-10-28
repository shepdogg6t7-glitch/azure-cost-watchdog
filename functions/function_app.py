"""Scheduled entrypoint: run the audit every night and publish the report.

Deployed as a Python 3.11 Azure Functions app on a consumption plan. Every Azure API call
(inventory, cost, metrics, actions) uses the user-assigned managed identity from
../infra, so no service-principal secret is stored anywhere. The Functions *host* still
needs its own storage connection string; see the README for the identity-based
replacement if you want that removed too.

Environment variables (set by infra/functionApp.bicep):
  WATCHDOG_SUBSCRIPTIONS   comma-separated subscription ids; empty = all visible
  WATCHDOG_DAYS            lookback window in days (default 30)
  WATCHDOG_POLICY          path to a policy JSON in the deployment package (default: built-in)
  WATCHDOG_APPLY           "true" to execute actions, anything else = report only
  WATCHDOG_ALLOW_PROD      "true" to execute production actions (still needs reviewer sign-off)
  WATCHDOG_ALLOW_ORPHANS   "true" to delete orphaned disks/IPs/snapshots
  WATCHDOG_REPORT_CONTAINER  blob container for reports + audit trail (default: reports)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

import azure.functions as func

from costwatchdog import evaluate
from costwatchdog.auditlog import AuditLog
from costwatchdog.azure_client import AzureClients, apply_plan
from costwatchdog.billing import summarize_costs
from costwatchdog.policy import Policy
from costwatchdog.report import render_json, render_markdown
from costwatchdog.sources import load_policy

app = func.FunctionApp()
LOGGER = logging.getLogger("costwatchdog.function")


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


@app.schedule(
    schedule="0 0 6 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def cost_watchdog(timer: func.TimerRequest) -> None:
    """Nightly audit: measure spend, find idle/orphaned resources, optionally act."""
    if timer.past_due:
        LOGGER.warning("timer is past due; running anyway")

    subscriptions = [item.strip() for item in os.environ.get("WATCHDOG_SUBSCRIPTIONS", "").split(",") if item.strip()]
    days = int(os.environ.get("WATCHDOG_DAYS", "30"))
    policy = _policy()
    apply_enabled = _truthy("WATCHDOG_APPLY")

    clients = AzureClients(subscriptions or None)
    resources = clients.list_resources()
    records = clients.fetch_cost_records(days=days)
    metrics = clients.fetch_usage_metrics(resources, days=days)
    summaries, unallocated = summarize_costs(records)

    result = evaluate(resources, summaries, metrics=metrics, policy=policy, unallocated=unallocated)
    scope = ", ".join(clients.subscription_ids)
    mode = "apply" if apply_enabled else "dry-run"

    _publish(render_markdown(result, mode=mode, scope=scope), f"costwatchdog-{_stamp()}.md")
    _publish(render_json(result, mode=mode, scope=scope), f"costwatchdog-{_stamp()}.json")

    LOGGER.info(
        "audit complete: %s decision(s), identified %s %s/mo, actionable %s %s/mo",
        len(result.decisions),
        result.currency,
        result.identified_monthly_waste,
        result.currency,
        result.actionable_monthly_savings,
    )

    if not apply_enabled:
        LOGGER.info("WATCHDOG_APPLY is not 'true'; no changes were made")
        return

    plan = result.to_dict()
    outcomes = apply_plan(
        plan,
        dry_run=False,
        allow_prod=_truthy("WATCHDOG_ALLOW_PROD"),
        allow_orphan_deletion=_truthy("WATCHDOG_ALLOW_ORPHANS"),
        limit=policy.max_actions_per_run,
        audit_log=AuditLog(None),  # trail goes to blob storage below
    )
    _publish(json.dumps(outcomes, indent=2), f"costwatchdog-actions-{_stamp()}.json")
    LOGGER.info(
        "apply: %s executed, %s skipped, %s failed",
        sum(1 for item in outcomes if item["status"] == "executed"),
        sum(1 for item in outcomes if item["status"] == "skipped"),
        sum(1 for item in outcomes if item["status"] == "failed"),
    )


def _policy() -> Policy:
    configured = os.environ.get("WATCHDOG_POLICY")
    if configured and os.path.exists(configured):
        return load_policy(configured)
    return Policy()


def _publish(content: str, name: str) -> None:
    """Write the artefact to blob storage; never fail the run because of publishing."""
    connection = os.environ.get("AzureWebJobsStorage")
    if not connection:
        LOGGER.warning("AzureWebJobsStorage is not set; skipping upload of %s", name)
        return
    try:
        from azure.storage.blob import BlobServiceClient

        container = os.environ.get("WATCHDOG_REPORT_CONTAINER", "reports")
        service = BlobServiceClient.from_connection_string(connection)
        blob = service.get_blob_client(container=container, blob=name)
        blob.upload_blob(content, overwrite=True)
        LOGGER.info("published %s", name)
    except Exception as error:  # noqa: BLE001 - reporting must not break the audit
        LOGGER.error("failed to publish %s: %s", name, error)


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
