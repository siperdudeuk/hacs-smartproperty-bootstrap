"""Build a Home Assistant inventory snapshot for the control plane.

Only reads from Home Assistant — no writes. Designed to be defensive so
that a failure to introspect one section (e.g. dashboards on a YAML-mode
setup) does not prevent reporting the rest.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

_LOGGER = logging.getLogger(__name__)


async def build_inventory(hass: HomeAssistant) -> dict[str, Any]:
    """Return a full snapshot of entities, devices, areas, dashboards, etc."""
    entities = _collect_entities(hass)
    return {
        "ha_version": hass.config.as_dict().get("version", "unknown"),
        "entities": entities,
        "devices": _collect_devices(hass),
        "areas": _collect_areas(hass),
        "dashboards": await _collect_dashboards(hass),
        "installed_frontend_resources": _collect_frontend_resources(hass),
        "automations": [e for e in entities if e["entity_id"].startswith("automation.")],
        "scripts": [e for e in entities if e["entity_id"].startswith("script.")],
        "scenes": [e for e in entities if e["entity_id"].startswith("scene.")],
    }


def _collect_entities(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Entity states enriched with registry data (device_id, area_id, platform).

    The bare state object does not know about the entity registry, so the
    template resolver on the control plane cannot map entities to rooms
    without this enrichment.
    """
    registry = er.async_get(hass)
    reg_by_id = {entry.entity_id: entry for entry in registry.entities.values()}

    result: list[dict[str, Any]] = []
    for s in hass.states.async_all():
        entry = reg_by_id.get(s.entity_id)
        item: dict[str, Any] = {
            "entity_id": s.entity_id,
            "state": s.state,
            "attributes": dict(s.attributes),
            "last_changed": s.last_changed.isoformat(),
            "last_updated": s.last_updated.isoformat(),
        }
        if entry is not None:
            if entry.device_id:
                item["device_id"] = entry.device_id
            if entry.area_id:
                item["area_id"] = entry.area_id
            if entry.platform:
                item["platform"] = entry.platform
            if entry.original_name:
                item["original_name"] = entry.original_name
        result.append(item)
    return result


def _collect_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    reg = dr.async_get(hass)
    return [
        {
            "id": d.id,
            "name": d.name,
            "name_by_user": d.name_by_user,
            "manufacturer": d.manufacturer,
            "model": d.model,
            "area_id": d.area_id,
        }
        for d in reg.devices.values()
    ]


def _collect_areas(hass: HomeAssistant) -> list[dict[str, Any]]:
    reg = ar.async_get(hass)
    return [{"id": a.id, "name": a.name} for a in reg.async_list_areas()]


def _collect_frontend_resources(hass: HomeAssistant) -> list[dict[str, Any]]:
    """Return installed Lovelace resources (custom frontend cards).

    Used by the control plane to know which HACS frontend cards are already
    present on the target HA before deploying a template that depends on a
    specific set. Best-effort: returns [] on unexpected internal layouts.
    """
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return []
    resources = getattr(lovelace, "resources", None)
    items = getattr(resources, "async_items", None)
    if callable(items):
        try:
            loaded = items()
            return [
                {
                    "url": r.get("url"),
                    "type": r.get("type"),
                    "id": r.get("id"),
                }
                for r in loaded
                if isinstance(r, dict) and r.get("url")
            ]
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to enumerate Lovelace resources: %s", exc)
    return []


async def _collect_dashboards(hass: HomeAssistant) -> dict[str, Any]:
    """Return {url_path: config} for every storage-mode dashboard we can load.

    Note: HA's lovelace internals have changed across versions. This reads
    best-effort from ``hass.data["lovelace"]`` and returns an empty dict on
    any unexpected layout — never raises.
    """
    result: dict[str, Any] = {}
    lovelace = hass.data.get("lovelace")
    if lovelace is None:
        return result

    dashboards = getattr(lovelace, "dashboards", None)
    if not dashboards:
        return result

    for url_path, dashboard in dashboards.items():
        key = url_path or "lovelace"
        try:
            config = await dashboard.async_load(False)
            if isinstance(config, dict):
                result[key] = config
        except Exception as exc:  # noqa: BLE001 — best effort introspection
            _LOGGER.debug("Skipping dashboard %s: %s", key, exc)

    return result
