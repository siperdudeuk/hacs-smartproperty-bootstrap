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


_PROTECTED_URL_PATHS = {None, "", "lovelace"}


async def update_dashboard_config(
    hass: HomeAssistant,
    url_path: str | None,
    config: dict[str, Any],
    *,
    title: str | None = None,
    icon: str | None = None,
    require_admin: bool = False,
    show_in_sidebar: bool = True,
) -> str:
    """Create-or-update a Lovelace dashboard, never touching the default.

    - Creates a new storage-mode dashboard if ``url_path`` is not yet registered.
    - Overwrites the config of an existing dashboard with the same ``url_path``.
    - Refuses to target the built-in default dashboard (``lovelace``/None): the
      control plane must always propose a dedicated slug so customer-curated
      dashboards are never clobbered.

    Returns the resolved ``url_path`` of the affected dashboard.
    """
    if url_path in _PROTECTED_URL_PATHS:
        raise DeployError(
            "Refusing to modify the default Lovelace dashboard. "
            "Provide a dedicated url_path (e.g. 'ai-generated-dash')."
        )

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        raise DeployError("Lovelace is not available on this HA instance.")

    dashboards = getattr(lovelace, "dashboards", None) or {}
    dashboard = dashboards.get(url_path)

    if dashboard is None:
        await _create_storage_dashboard(
            hass,
            url_path,
            title=title or url_path,
            icon=icon or "mdi:view-dashboard",
            require_admin=require_admin,
            show_in_sidebar=show_in_sidebar,
        )
        dashboard = getattr(lovelace, "dashboards", {}).get(url_path)
        if dashboard is None:
            raise DeployError(
                f"Dashboard {url_path!r} was created but not registered on hass.data."
            )

    try:
        await dashboard.async_save(config)
    except Exception as exc:
        raise DeployError(f"Failed to save dashboard {url_path!r}: {exc}") from exc

    return url_path


async def _create_storage_dashboard(
    hass: HomeAssistant,
    url_path: str,
    *,
    title: str,
    icon: str,
    require_admin: bool,
    show_in_sidebar: bool,
) -> None:
    # HA no longer exposes the running DashboardsCollection on hass.data
    # (2025.x+). Construct our own collection — it shares the same Store
    # backing file — then wire the new entry into hass.data["lovelace"]
    # and the panel registry so the dashboard is visible without a restart.
    try:
        from homeassistant.components import frontend
        from homeassistant.components.lovelace import dashboard as ll_dashboard
    except ImportError as exc:
        raise DeployError(f"Lovelace internals unavailable: {exc}") from exc

    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        raise DeployError("Lovelace is not available on this HA instance.")

    collection = ll_dashboard.DashboardsCollection(hass)
    try:
        await collection.async_load()
    except Exception as exc:
        raise DeployError(f"Failed to load dashboards collection: {exc}") from exc

    try:
        item = await collection.async_create_item(
            {
                "url_path": url_path,
                "title": title,
                "icon": icon,
                "require_admin": require_admin,
                "show_in_sidebar": show_in_sidebar,
                "mode": "storage",
            }
        )
    except Exception as exc:
        raise DeployError(f"Failed to create dashboard {url_path!r}: {exc}") from exc

    lovelace.dashboards[url_path] = ll_dashboard.LovelaceStorage(hass, item)

    try:
        frontend.async_register_built_in_panel(
            hass,
            "lovelace",
            sidebar_title=title,
            sidebar_icon=icon,
            frontend_url_path=url_path,
            config={"mode": "storage"},
            require_admin=require_admin,
            update=False,
            show_in_sidebar=show_in_sidebar,
        )
    except Exception as exc:
        raise DeployError(
            f"Failed to register frontend panel for {url_path!r}: {exc}"
        ) from exc


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
