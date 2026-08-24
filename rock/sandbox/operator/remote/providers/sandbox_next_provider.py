"""SandboxNext provider — talks to the SandboxNext Gateway REST API.

Implements the RemoteProvider Protocol using httpx.AsyncClient.
See docs/proposals/sandbox-next.yaml for the OpenAPI spec.
"""

from __future__ import annotations

from typing import Any

import httpx


from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.logger import init_logger
from rock.sandbox.operator.remote.constants import EXT_BACKEND, EXT_ENDPOINT, BACKEND_NAME

logger = init_logger(__name__)

# --- SandboxNext SandboxState -> Rock State ---

_DEFAULT_STATE_MAP: dict[str, State] = {
    "creating": State.PENDING,
    "running": State.RUNNING,
    "pausing": State.STOPPED,
    "paused": State.STOPPED,
    "resuming": State.PENDING,
    "failed": State.STOPPED,
}


def _map_state(sn_state: str | None, state_map: dict[str, State] | None = None) -> State:
    table = state_map or _DEFAULT_STATE_MAP
    return table.get(sn_state or "", State.PENDING)


def _parse_mem_to_mb(mem: str) -> int:
    """Convert docker-style memory string (``8g``/``4096m``/``2048``) to MB."""
    s = mem.strip().lower()
    if not s:
        return 0
    if s.endswith("g"):
        return int(float(s[:-1]) * 1024)
    if s.endswith("m"):
        return int(float(s[:-1]))
    return int(float(s))


def _parse_disk_to_mb(disk: str | None) -> int:
    """Convert docker-style disk string (``50G``/``51200M``) to MB."""
    if not disk:
        return 0
    return _parse_mem_to_mb(disk)


class SandboxNextProvider:
    """Provider that talks to the SandboxNext Gateway REST API."""

    def __init__(self, config: RemoteOperatorConfig, *, client: httpx.AsyncClient | None = None):
        self._config = config
        opts = config.provider_options
        self._state_map = opts.get("state_mapping") or _DEFAULT_STATE_MAP
        self._retry_max = opts.get("retry_max", 3)
        self._retry_backoff = opts.get("retry_backoff_base", 0.5)
        self._region = opts.get("region", "cn-hangzhou")
        self._sandbox_class = opts.get("sandbox_class", "headless-vm")

        base_url = config.base_url
        headers: dict[str, str] = {}
        if config.api_key:
            headers["X-Api-Key"] = config.api_key
        if config.access_token:
            headers["Authorization"] = f"Bearer {config.access_token}"

        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            headers=headers,
            timeout=config.default_timeout,
        )
        logger.info("Initialized SandboxNextProvider (base_url=%s, region=%s)", config.base_url, self._region)

    # --- HTTP helpers ---

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send an HTTP request with limited retry on 5xx errors."""
        response = await self._client.request(method, path, **kwargs)
        retry_count = 0
        while response.status_code >= 500 and retry_count < self._retry_max:
            retry_count += 1
            import asyncio

            await asyncio.sleep(self._retry_backoff * (2 ** (retry_count - 1)))
            response = await self._client.request(method, path, **kwargs)
        return response

    # --- Lifecycle ---

    async def submit(self, config: DockerDeploymentConfig, user_info: dict) -> SandboxInfo:
        sandbox_id = config.container_name
        user_id = user_info.get("user_id", "default")
        experiment_id = user_info.get("experiment_id", "default")
        namespace = user_info.get("namespace", "default")

        body: dict[str, Any] = {
            "request_id": sandbox_id,
            "region": self._region,
            "class": self._sandbox_class,
            "resources": {
                "vcpu": int(config.cpus),
                "memory_mb": _parse_mem_to_mb(config.memory),
                "disk_mb": _parse_disk_to_mb(config.disk),
            },
            "metadata": {
                "rock_sandbox_id": sandbox_id or "",
                "user_id": user_id,
                "experiment_id": experiment_id,
                "namespace": namespace,
            },
        }
        if config.template_id:
            body["template_id"] = config.template_id
        if config.env_vars:
            body["env_vars"] = config.env_vars

        response = await self._request("POST", "/v1/sandboxes", json=body)
        response.raise_for_status()
        data = response.json()

        sn_id = data["sandbox_id"]
        sn_state = data.get("state")
        access = data.get("access") or {}
        endpoint_template = access.get("endpoint_template", "")
        agent_token = access.get("agent_token", "")

        logger.info("[%s] sandbox_next submitted, remote_id=%s, state=%s", sandbox_id, sn_id, sn_state)

        info: SandboxInfo = {
            "sandbox_id": sandbox_id,
            "host_name": sn_id,
            "image": config.image,
            "cpus": config.cpus,
            "memory": config.memory,
            "user_id": user_id,
            "experiment_id": experiment_id,
            "namespace": namespace,
            "state": _map_state(sn_state, self._state_map),
            "host_ip": endpoint_template,
            "port_mapping": {
                Port.PROXY: 8000,
                Port.SERVER: 8080,
                Port.SSH: 22,
            },
            "auth_token": agent_token,
            "extended_params": {
                EXT_BACKEND: BACKEND_NAME,
                EXT_ENDPOINT: endpoint_template,
            },
        }
        return info

    async def get_status(self, remote_sandbox_id: str) -> SandboxInfo | None:
        response = await self._request("GET", f"/v1/sandboxes/{remote_sandbox_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        data = response.json()

        sn_state = data.get("state")
        access = data.get("access") or {}
        endpoint_template = access.get("endpoint_template", "")
        agent_token = access.get("agent_token", "")

        # Only return fields that change at runtime; static fields are already in redis.
        info: SandboxInfo = {
            "state": _map_state(sn_state, self._state_map),
            "host_ip": endpoint_template,
            "auth_token": agent_token,
        }
        return info

    async def stop(self, remote_sandbox_id: str) -> bool:
        """Stop the sandbox by deleting it."""
        return await self.delete(remote_sandbox_id)

    async def delete(self, remote_sandbox_id: str) -> bool:
        response = await self._request("DELETE", f"/v1/sandboxes/{remote_sandbox_id}")
        if response.status_code == 404:
            return True
        response.raise_for_status()
        return True

    # --- Template API (not implemented for SandboxNext yet) ---

    async def create_template(self, spec: Any) -> dict:
        raise NotImplementedError("template API is not supported by SandboxNextProvider yet")

    async def get_template_status(self, template_id: str) -> dict | None:
        raise NotImplementedError("template API is not supported by SandboxNextProvider yet")

    async def delete_template(self, template_id: str) -> bool:
        raise NotImplementedError("template API is not supported by SandboxNextProvider yet")
