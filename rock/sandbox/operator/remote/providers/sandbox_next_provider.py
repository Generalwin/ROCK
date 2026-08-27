"""SandboxNext provider — talks to the SandboxManager Control HTTP API.

Implements the RemoteProvider Protocol using httpx.AsyncClient.
See docs/proposals/sandbox-next.yaml for the OpenAPI spec. The owning profile is
passed via the ``X-Sandbox-Profile-ID`` header on every request; region and
class concepts live in headers too, not in request bodies.
"""

from __future__ import annotations

import asyncio
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

# --- SandboxManager SandboxState -> Rock State ---

_DEFAULT_STATE_MAP: dict[str, State] = {
    "SANDBOX_STATE_UNSPECIFIED": State.PENDING,
    "SANDBOX_CREATING": State.PENDING,
    "SANDBOX_ALLOCATED": State.PENDING,
    "SANDBOX_RUNNING": State.RUNNING,
    "SANDBOX_PAUSING": State.STOPPED,
    "SANDBOX_PAUSED": State.STOPPED,
    "SANDBOX_PAUSE_FAILED": State.STOPPED,
    "SANDBOX_RESUMING": State.PENDING,
    "SANDBOX_RESUME_FAILED": State.STOPPED,
    "SANDBOX_DELETING": State.STOPPED,
    "SANDBOX_DELETED": State.DELETED,
    "SANDBOX_FAILED": State.STOPPED,
    "SANDBOX_UNKNOWN": State.PENDING,
    "SANDBOX_MIGRATING": State.PENDING,
}

# Control HTTP API headers
PROFILE_ID_HEADER = "X-Sandbox-Profile-ID"
CLASS_HEADER = "X-Sandbox-Class"


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
    """Provider that talks to the SandboxManager Control HTTP API."""

    def __init__(self, config: RemoteOperatorConfig, *, client: httpx.AsyncClient | None = None):
        self._config = config
        opts = config.provider_options
        self._state_map = opts.get("state_mapping") or _DEFAULT_STATE_MAP
        self._retry_max = opts.get("retry_max", 3)
        self._retry_backoff = opts.get("retry_backoff_base", 0.5)
        self._profile_id = opts.get("profile_id", "")
        self._sandbox_class = opts.get("sandbox_class", "")
        if not self._profile_id:
            raise ValueError("provider_options.profile_id is required (X-Sandbox-Profile-ID header)")

        base_url = config.base_url
        headers: dict[str, str] = {PROFILE_ID_HEADER: self._profile_id}
        if self._sandbox_class:
            headers[CLASS_HEADER] = self._sandbox_class
        if config.api_key:
            headers["X-Api-Key"] = config.api_key
        if config.access_token:
            headers["Authorization"] = f"Bearer {config.access_token}"

        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=config.default_timeout,
        )
        # Auth / routing headers are provider-level; apply even to an injected client.
        self._client.headers.update(headers)
        logger.info(
            "Initialized SandboxNextProvider (base_url=%s, profile_id=%s, class=%s)",
            config.base_url,
            self._profile_id,
            self._sandbox_class,
        )

    # --- HTTP helpers ---

    async def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Send an HTTP request with limited retry on 5xx errors."""
        response = await self._client.request(method, path, **kwargs)
        retry_count = 0
        while response.status_code >= 500 and retry_count < self._retry_max:
            retry_count += 1
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
            "resource_spec": {
                "vcpu_count": int(config.cpus),
                "memory_mb": _parse_mem_to_mb(config.memory),
                "disk_size_mb": _parse_disk_to_mb(config.disk),
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
        endpoint = data.get("endpoint") or ""

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
            "host_ip": endpoint,
            "port_mapping": {
                Port.PROXY: 8000,
                Port.SERVER: 8080,
                Port.SSH: 22,
            },
            "extended_params": {
                EXT_BACKEND: BACKEND_NAME,
                EXT_ENDPOINT: endpoint,
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
        endpoint = data.get("endpoint") or ""

        # Only return fields that change at runtime; static fields are already in redis.
        info: SandboxInfo = {
            "state": _map_state(sn_state, self._state_map),
            "host_ip": endpoint,
        }
        return info

    async def stop(self, remote_sandbox_id: str) -> bool:
        """Stop the sandbox by deleting it (Rock does not use pause/resume)."""
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
