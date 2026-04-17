"""Config flow for Smart Property Software Bootstrap."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    AGENT_VERSION,
    CONF_CLIENT_ID,
    CONF_CONTROL_PLANE_URL,
    CONF_INSTANCE_ID,
    CONF_INSTANCE_NAME,
    CONF_PAIR_CODE,
    CONF_POLL_INTERVAL,
    CONF_SIGNING_KEY,
    CONF_SYNC_INTERVAL,
    CONF_TOKEN,
    DEFAULT_CONTROL_PLANE_URL,
    DEFAULT_POLL_INTERVAL,
    DEFAULT_SYNC_INTERVAL,
    DOMAIN,
    MAX_POLL_INTERVAL,
    MIN_POLL_INTERVAL,
)
from .control_plane import ControlPlaneClient, ControlPlaneError
from .ha_ops import get_ha_version

_LOGGER = logging.getLogger(__name__)


def _user_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema(
        {
            vol.Required(CONF_PAIR_CODE): str,
            vol.Optional(
                CONF_INSTANCE_NAME,
                default=d.get(CONF_INSTANCE_NAME, "Home Assistant"),
            ): str,
            vol.Optional(
                CONF_CONTROL_PLANE_URL,
                default=d.get(CONF_CONTROL_PLANE_URL, DEFAULT_CONTROL_PLANE_URL),
            ): str,
        }
    )


class SPSBootstrapConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """User-initiated pair-code flow."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            pair_code = user_input[CONF_PAIR_CODE].strip()
            instance_name = (
                user_input[CONF_INSTANCE_NAME].strip() or "Home Assistant"
            )
            base_url = (
                user_input[CONF_CONTROL_PLANE_URL].strip()
                or DEFAULT_CONTROL_PLANE_URL
            )

            try:
                paired = await _pair(
                    self.hass,
                    pair_code=pair_code,
                    instance_name=instance_name,
                    base_url=base_url,
                )
            except ControlPlaneError as exc:
                _LOGGER.warning("Pairing failed: %s", exc)
                errors["base"] = "pair_failed"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected pairing error")
                errors["base"] = "unknown"
            else:
                await self.async_set_unique_id(paired["instanceId"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=instance_name,
                    data={
                        CONF_CONTROL_PLANE_URL: base_url,
                        CONF_INSTANCE_ID: paired["instanceId"],
                        CONF_TOKEN: paired["token"],
                        CONF_SIGNING_KEY: paired.get("signingKey"),
                        CONF_CLIENT_ID: paired.get("clientId"),
                        CONF_INSTANCE_NAME: instance_name,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_user_schema(user_input),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        return SPSBootstrapOptionsFlow(config_entry)


class SPSBootstrapOptionsFlow(config_entries.OptionsFlow):
    """Lets users tune sync + poll intervals without re-pairing."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        opts = self._config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        CONF_POLL_INTERVAL,
                        default=opts.get(CONF_POLL_INTERVAL, DEFAULT_POLL_INTERVAL),
                    ): vol.All(
                        int, vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                    ),
                    vol.Optional(
                        CONF_SYNC_INTERVAL,
                        default=opts.get(CONF_SYNC_INTERVAL, DEFAULT_SYNC_INTERVAL),
                    ): vol.All(
                        int, vol.Range(min=MIN_POLL_INTERVAL, max=MAX_POLL_INTERVAL)
                    ),
                }
            ),
        )


async def _pair(
    hass: HomeAssistant,
    *,
    pair_code: str,
    instance_name: str,
    base_url: str,
) -> dict[str, Any]:
    session = async_get_clientsession(hass)
    client = ControlPlaneClient(session, base_url)
    return await client.pair(
        pair_code=pair_code,
        instance_name=instance_name,
        ha_version=get_ha_version(hass),
        agent_version=AGENT_VERSION,
    )
