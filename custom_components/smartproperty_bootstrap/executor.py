"""Dispatch signed control plane jobs to Home Assistant operations."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from homeassistant.core import HomeAssistant

from .ha_ops import DeployError, apply_rollback, update_dashboard_config, upsert_automation

_LOGGER = logging.getLogger(__name__)


@dataclass
class JobResult:
    ok: bool
    message: str
    details: dict[str, Any] | None = None


def verify_signature(
    *,
    job_id: str,
    instance_id: str,
    nonce: str,
    payload: dict[str, Any],
    signature: str,
    signing_secret: str,
) -> bool:
    """Verify HMAC-SHA256 signature: sha256(jobId.instanceId.nonce.sha256(payload))."""
    payload_hash = hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()
    ).hexdigest()
    message = f"{job_id}.{instance_id}.{nonce}.{payload_hash}"
    expected = hmac.new(
        signing_secret.encode(), message.encode(), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


async def execute_job(
    hass: HomeAssistant,
    job: dict[str, Any],
    *,
    instance_id: str,
    signing_secret: str,
) -> JobResult:
    job_id = str(job.get("id", "?"))
    operation = str(job.get("operation", ""))
    payload = job.get("payload") or {}
    if not isinstance(payload, dict):
        return JobResult(False, "Job payload is malformed.")

    nonce = str(job.get("nonce", ""))
    signature = str(job.get("signature", ""))
    if signing_secret and signature and not verify_signature(
        job_id=job_id,
        instance_id=instance_id,
        nonce=nonce,
        payload=payload,
        signature=signature,
        signing_secret=signing_secret,
    ):
        return JobResult(False, "HMAC signature verification failed.")

    _LOGGER.info("Executing job %s (operation=%s)", job_id, operation)

    handler = _HANDLERS.get(operation)
    if handler is None:
        return JobResult(False, f"Unknown operation: {operation}")

    try:
        return await handler(hass, payload)
    except DeployError as exc:
        return JobResult(False, str(exc))
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("Job %s failed", job_id)
        return JobResult(False, f"Handler error: {exc}")


async def _handle_dashboard(hass: HomeAssistant, payload: dict[str, Any]) -> JobResult:
    url_path = payload.get("url_path") or "lovelace"
    config = payload.get("config")
    if not isinstance(config, dict):
        return JobResult(False, "dashboard_proposal: config is required.")
    await update_dashboard_config(hass, url_path, config)
    return JobResult(
        True, f"Dashboard '{url_path}' updated.", {"url_path": url_path}
    )


async def _handle_automation(hass: HomeAssistant, payload: dict[str, Any]) -> JobResult:
    automation_id = payload.get("automation_id")
    config = payload.get("config")
    if not isinstance(automation_id, str) or not automation_id:
        return JobResult(False, "automation_proposal: automation_id is required.")
    if not isinstance(config, dict):
        return JobResult(False, "automation_proposal: config is required.")
    await upsert_automation(hass, automation_id, config)
    return JobResult(
        True, f"Automation '{automation_id}' deployed.", {"automation_id": automation_id}
    )


async def _handle_rollback(hass: HomeAssistant, payload: dict[str, Any]) -> JobResult:
    inventory = payload.get("inventory")
    if not isinstance(inventory, dict):
        return JobResult(False, "rollback: inventory is required.")
    restored = await apply_rollback(hass, inventory)
    reason = payload.get("reason", "rollback")
    return JobResult(
        True,
        f"Rollback applied ({reason}). Restored {len(restored)} items.",
        {"restored": restored},
    )


Handler = Callable[[HomeAssistant, dict[str, Any]], Awaitable[JobResult]]

_HANDLERS: dict[str, Handler] = {
    "dashboard_proposal": _handle_dashboard,
    "automation_proposal": _handle_automation,
    "rollback": _handle_rollback,
}
