"""Internal Home Assistant operations for applying control plane jobs.

This is the HA-internals surface of the integration. APIs here are
version-sensitive; isolate churn to this module.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

_AUTOMATIONS_FILE = "automations.yaml"


class DeployError(Exception):
    """Raised when an HA write operation cannot be completed."""


# ── Dashboards ────────────────────────────────────────────────────────


async def update_dashboard_config(
    hass: HomeAssistant, url_path: str | None, config: dict[str, Any]
) -> None:
    """Overwrite an existing Lovelace dashboard's config.

    Pass ``url_path=None`` or ``"lovelace"`` for the default overview.
    v0.1 only updates existing dashboards — the slot must be created in HA
    by the user before a proposal can target it.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        raise DeployError("Lovelace is not available on this HA instance.")

    dashboards = getattr(lovelace, "dashboards", None) or {}
    key = None if url_path in (None, "", "lovelace") else url_path

    dashboard = dashboards.get(key)
    if dashboard is None:
        raise DeployError(
            f"Dashboard '{url_path or 'overview'}' does not exist. "
            "Create the dashboard slot in Home Assistant before deploying."
        )

    try:
        await dashboard.async_save(config)
    except Exception as exc:
        raise DeployError(f"Failed to save dashboard {url_path!r}: {exc}") from exc


# ── Automations ───────────────────────────────────────────────────────


async def upsert_automation(
    hass: HomeAssistant, automation_id: str, config: dict[str, Any]
) -> None:
    """Insert or update an automation in automations.yaml, then reload."""
    path = Path(hass.config.path(_AUTOMATIONS_FILE))
    await hass.async_add_executor_job(
        _write_automation_sync, path, automation_id, config
    )
    await hass.services.async_call("automation", "reload", blocking=True)


def _write_automation_sync(
    path: Path, automation_id: str, config: dict[str, Any]
) -> None:
    existing: list[dict[str, Any]] = []
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            loaded = yaml.safe_load(fh)
        if isinstance(loaded, list):
            existing = loaded

    entry = {**config, "id": automation_id}

    replaced = False
    for i, item in enumerate(existing):
        if isinstance(item, dict) and str(item.get("id")) == automation_id:
            existing[i] = entry
            replaced = True
            break
    if not replaced:
        existing.append(entry)

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(existing, fh, sort_keys=False, allow_unicode=True)


# ── Rollback ──────────────────────────────────────────────────────────


async def apply_rollback(
    hass: HomeAssistant, inventory: dict[str, Any]
) -> list[str]:
    """Restore dashboards and automations from an inventory snapshot."""
    restored: list[str] = []

    dashboards = inventory.get("dashboards", {})
    if isinstance(dashboards, dict):
        for url_path, config in dashboards.items():
            if not isinstance(config, dict):
                continue
            try:
                await update_dashboard_config(hass, url_path, config)
                restored.append(f"dashboard:{url_path}")
            except DeployError as exc:
                _LOGGER.warning("Rollback skipped dashboard %s: %s", url_path, exc)

    automations = inventory.get("automations", [])
    if isinstance(automations, list):
        for auto in automations:
            if not isinstance(auto, dict):
                continue
            entity_id = auto.get("entity_id", "")
            auto_id = entity_id.replace("automation.", "") if entity_id else None
            config = auto.get("attributes")
            if auto_id and isinstance(config, dict):
                try:
                    await upsert_automation(hass, auto_id, config)
                    restored.append(f"automation:{auto_id}")
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning("Rollback skipped automation %s: %s", auto_id, exc)

    return restored


def get_ha_version(hass: HomeAssistant) -> str:
    return hass.config.as_dict().get("version", "unknown")
