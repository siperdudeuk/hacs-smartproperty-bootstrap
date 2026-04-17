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
    url_path = payload.get("url_path")
    config = payload.get("config")
    if not isinstance(url_path, str) or not url_path:
        return JobResult(
            False,
            "dashboard_proposal: url_path is required (must be a dedicated slug, not the default).",
        )
    if not isinstance(config, dict):
        return JobResult(False, "dashboard_proposal: config is required.")

    title = payload.get("title") if isinstance(payload.get("title"), str) else None
    icon = payload.get("icon") if isinstance(payload.get("icon"), str) else None
    require_admin = bool(payload.get("require_admin", False))
    show_in_sidebar = bool(payload.get("show_in_sidebar", True))

    resolved = await update_dashboard_config(
        hass,
        url_path,
        config,
        title=title,
        icon=icon,
        require_admin=require_admin,
        show_in_sidebar=show_in_sidebar,
    )
    return JobResult(
        True, f"Dashboard '{resolved}' deployed.", {"url_path": resolved}
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


async def _handle_resource_check(hass: HomeAssistant, payload: dict[str, Any]) -> JobResult:
    """Verify a list of HACS frontend card repos are installed on this HA.

    Payload:
      ``required``: list of HACS repository identifiers (e.g. 'mushroom', 'bubble-card').

    The check matches against Lovelace resource URLs — typically containing
    the card's file name. Missing cards are returned so the admin flow can
    ask the customer to install them via HACS before deploying the template.
    """
    required = payload.get("required") or []
    if not isinstance(required, list):
        return JobResult(False, "resource_check: 'required' must be a list.")

    lovelace = hass.data.get("lovelace")
    resources = getattr(lovelace, "resources", None) if lovelace else None
    items_fn = getattr(resources, "async_items", None)
    urls: list[str] = []
    if callable(items_fn):
        try:
            for r in items_fn() or []:
                if isinstance(r, dict) and isinstance(r.get("url"), str):
                    urls.append(r["url"].lower())
        except Exception:  # noqa: BLE001
            pass

    missing: list[str] = []
    present: list[str] = []
    for repo in required:
        if not isinstance(repo, str):
            continue
        needle = repo.lower().replace("_", "-")
        match = any(needle in url for url in urls)
        (present if match else missing).append(repo)

    if missing:
        return JobResult(
            False,
            f"Missing frontend cards: {', '.join(missing)}",
            {"missing": missing, "present": present},
        )
    return JobResult(True, "All required frontend cards are installed.", {"present": present})


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
    "resource_check": _handle_resource_check,
    "rollback": _handle_rollback,
}
