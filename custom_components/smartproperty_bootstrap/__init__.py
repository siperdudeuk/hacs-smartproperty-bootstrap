"""Smart Property Software Bootstrap integration."""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_CONTROL_PLANE_URL,
    CONF_INSTANCE_ID,
    CONF_POLL_INTERVAL,
    CONF_SIGNING_KEY,
    CONF_SYNC_INTERVAL,
    CONF_TOKEN,
    DEFAULT_CONTROL_PLANE_URL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
)
from .control_plane import ControlPlaneClient
from .coordinator import SPSCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Start the agent loop for a paired instance."""
    base_url = entry.data.get(CONF_CONTROL_PLANE_URL, DEFAULT_CONTROL_PLANE_URL)
    instance_id = entry.data[CONF_INSTANCE_ID]
    token = entry.data[CONF_TOKEN]
    signing_key = entry.data.get(CONF_SIGNING_KEY)
    poll_interval = entry.options.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL)

    session = async_get_clientsession(hass)
    client = ControlPlaneClient(
        session,
        base_url,
        instance_id=instance_id,
        token=token,
        signing_key=signing_key,
    )

    coordinator = SPSCoordinator(
        hass,
        client=client,
        poll_interval=poll_interval,
        sync_interval=entry.options.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL),
    )

    await coordinator.async_config_entry_first_refresh()

    # DataUpdateCoordinator stops its internal scheduler when no entities are
    # subscribed. This integration exposes no platforms, so drive refreshes
    # explicitly via async_track_time_interval.
    async def _tick(_now) -> None:
        await coordinator.async_request_refresh()

    entry.async_on_unload(
        async_track_time_interval(hass, _tick, timedelta(seconds=poll_interval))
    )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator: SPSCoordinator | None = (
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    )
    if coordinator is not None:
        await coordinator.async_shutdown()
    return True


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
