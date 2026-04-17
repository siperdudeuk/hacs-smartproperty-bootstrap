"""Async HTTP client for the Smart Property Software control plane."""

from __future__ import annotations

import logging
from typing import Any

from aiohttp import ClientResponse, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)

_PAIR_TIMEOUT = ClientTimeout(total=30)
_SYNC_TIMEOUT = ClientTimeout(total=60)
_POLL_TIMEOUT = ClientTimeout(total=30)


class ControlPlaneError(Exception):
    """Raised when the control plane returns an error or is unreachable."""


class ControlPlaneClient:
    """Authenticated client for the SPS control plane API."""

    def __init__(
        self,
        session: ClientSession,
        base_url: str,
        *,
        instance_id: str | None = None,
        token: str | None = None,
        signing_key: str | None = None,
    ) -> None:
        self._session = session
        self._base_url = base_url.rstrip("/")
        self.instance_id = instance_id
        self.token = token
        self.signing_key = signing_key

    @property
    def is_paired(self) -> bool:
        return bool(self.instance_id and self.token)

    async def pair(
        self,
        *,
        pair_code: str,
        instance_name: str,
        ha_version: str | None = None,
        agent_version: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"pairCode": pair_code, "instanceName": instance_name}
        if ha_version:
            body["haVersion"] = ha_version
        if agent_version:
            body["agentVersion"] = agent_version

        data = await self._post("/api/ha/agent/pair", body, timeout=_PAIR_TIMEOUT)
        self.instance_id = data["instanceId"]
        self.token = data["token"]
        self.signing_key = data.get("signingKey")
        return data

    async def sync(self, inventory: dict[str, Any]) -> dict[str, Any]:
        self._require_paired()
        return await self._post(
            "/api/ha/agent/sync",
            {
                "instanceId": self.instance_id,
                "token": self.token,
                "inventory": inventory,
            },
            timeout=_SYNC_TIMEOUT,
        )

    async def get_pending_jobs(self) -> list[dict[str, Any]]:
        self._require_paired()
        url = f"{self._base_url}/api/ha/agent/jobs"
        params = {"instanceId": self.instance_id, "token": self.token}
        try:
            async with self._session.get(url, params=params, timeout=_POLL_TIMEOUT) as resp:
                data = await self._read_json(resp)
        except ControlPlaneError:
            raise
        except Exception as exc:  # aiohttp connection errors, JSON decode, etc.
            raise ControlPlaneError(f"Failed to fetch jobs: {exc}") from exc
        jobs = data.get("jobs", [])
        return jobs if isinstance(jobs, list) else []

    async def report_job_result(
        self,
        *,
        job_id: str,
        ok: bool,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_paired()
        body: dict[str, Any] = {
            "instanceId": self.instance_id,
            "token": self.token,
            "jobId": job_id,
            "ok": ok,
            "message": message,
        }
        if details:
            body["details"] = details
        return await self._post("/api/ha/agent/job-result", body, timeout=_POLL_TIMEOUT)

    def _require_paired(self) -> None:
        if not self.is_paired:
            raise ControlPlaneError("Control plane client is not paired.")

    async def _post(
        self, path: str, body: dict[str, Any], *, timeout: ClientTimeout
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with self._session.post(url, json=body, timeout=timeout) as resp:
                return await self._read_json(resp)
        except ControlPlaneError:
            raise
        except Exception as exc:
            raise ControlPlaneError(f"POST {path} failed: {exc}") from exc

    @staticmethod
    async def _read_json(resp: ClientResponse) -> dict[str, Any]:
        if resp.status >= 400:
            text = await resp.text()
            raise ControlPlaneError(f"HTTP {resp.status}: {text[:500]}")
        return await resp.json(content_type=None)
