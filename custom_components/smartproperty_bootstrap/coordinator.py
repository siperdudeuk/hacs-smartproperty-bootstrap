"""Periodic inventory sync + job polling against the SPS control plane."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import AGENT_VERSION
from .control_plane import ControlPlaneClient, ControlPlaneError
from .executor import execute_job
from .ha_ops import get_ha_version
from .inventory import build_inventory

_LOGGER = logging.getLogger(__name__)


def _version_tuple(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in v.strip().split("."))
    except (ValueError, AttributeError):
        return (0,)


class SPSCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Runs the agent loop: sync inventory, then poll + execute jobs."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        client: ControlPlaneClient,
        poll_interval: int,
        sync_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name="smartproperty_bootstrap",
            update_interval=timedelta(seconds=poll_interval),
        )
        self._client = client
        self._sync_interval = max(sync_interval, poll_interval)
        self._last_sync_monotonic: float = 0.0
        self._notified_version: str | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        sync_summary = await self._maybe_sync()

        try:
            poll = await self._client.get_pending_jobs()
        except ControlPlaneError as exc:
            raise UpdateFailed(f"Job poll failed: {exc}") from exc

        jobs = poll.get("jobs", [])
        latest = poll.get("latest_agent_version")
        await self._maybe_notify_update(latest)

        job_summaries = [await self._run_job(job) for job in jobs]

        return {
            "sync": sync_summary,
            "jobs": job_summaries,
            "ha_version": get_ha_version(self.hass),
            "agent_version": AGENT_VERSION,
            "latest_agent_version": latest,
        }

    async def _maybe_notify_update(self, latest: Any) -> None:
        if not isinstance(latest, str) or not latest:
            return
        if latest == AGENT_VERSION or latest == self._notified_version:
            return
        # Only notify when the server reports a strictly newer version.
        if _version_tuple(latest) <= _version_tuple(AGENT_VERSION):
            return
        self._notified_version = latest
        await self.hass.services.async_call(
            "persistent_notification",
            "create",
            {
                "notification_id": "smartproperty_bootstrap_update",
                "title": "Smart Property Software update available",
                "message": (
                    f"A newer agent version ({latest}) is available. "
                    f"Open HACS → Smart Property Software Bootstrap → "
                    f"Redownload to install. Current: {AGENT_VERSION}."
                ),
            },
            blocking=False,
        )

    async def _maybe_sync(self) -> dict[str, Any] | None:
        if not self._should_sync():
            return None
        try:
            inventory = await build_inventory(self.hass)
            summary = await self._client.sync(inventory)
            self._last_sync_monotonic = self.hass.loop.time()
            return summary
        except ControlPlaneError as exc:
            _LOGGER.warning("Inventory sync failed: %s", exc)
            return None

    def _should_sync(self) -> bool:
        if self._last_sync_monotonic == 0.0:
            return True
        return (
            self.hass.loop.time() - self._last_sync_monotonic
        ) >= self._sync_interval

    async def _run_job(self, job: dict[str, Any]) -> dict[str, Any]:
        job_id = str(job.get("id", "?"))
        operation = str(job.get("operation", "?"))
        result = await execute_job(
            self.hass,
            job,
            instance_id=self._client.instance_id or "",
            signing_secret=self._client.signing_key or self._client.token or "",
        )
        try:
            await self._client.report_job_result(
                job_id=job_id,
                ok=result.ok,
                message=result.message,
                details=result.details,
            )
        except ControlPlaneError as exc:
            _LOGGER.error("Failed to report job %s result: %s", job_id, exc)

        log = _LOGGER.info if result.ok else _LOGGER.warning
        log(
            "Job %s (%s): %s — %s",
            job_id,
            operation,
            "OK" if result.ok else "FAILED",
            result.message,
        )
        return {
            "id": job_id,
            "operation": operation,
            "ok": result.ok,
            "message": result.message,
        }
