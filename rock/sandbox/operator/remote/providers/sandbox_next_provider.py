"""SandboxNext provider — talks to the SandboxManager Control HTTP API.

Implements the RemoteProvider Protocol using httpx.AsyncClient. Auth is the
``X-Api-Key`` header — the gateway resolves the owning profile from the key
and forwards it internally. ``X-Sandbox-Class`` routes to a cell.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx


from rock.actions.sandbox.config import RemoteSandboxRuntimeConfig
from rock.actions.sandbox.response import State
from rock.actions.sandbox.sandbox_info import SandboxInfo
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.deployments.constants import Port
from rock.logger import init_logger
from rock.sandbox.operator.k8s.template_loader import K8sTemplateLoader
from rock.sandbox.operator.remote.constants import EXT_BACKEND, EXT_ENDPOINT, EXT_USE_RAW, EXT_USE_RAW_ENABLED, BACKEND_NAME
from rock.sandbox.remote_sandbox import RemoteSandboxRuntime
from rock.utils.format import normalize_memory_to_k8s, parse_size_to_mb

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
API_KEY_HEADER = "X-Api-Key"
CLASS_HEADER = "X-Sandbox-Class"

# Fixed port mapping for the template/resource_spec path (raw mode reads ports from the template)
_DEFAULT_PORT_MAPPING: dict[int, int] = {
    Port.PROXY: 8000,
    Port.SERVER: 8080,
    Port.SSH: 22,
}


def _map_state(sn_state: str | None, state_map: dict[str, State] | None = None) -> State:
    table = state_map or _DEFAULT_STATE_MAP
    return table.get(sn_state or "", State.PENDING)


def _derive_sandbox_class(config: DockerDeploymentConfig) -> str:
    """Derive X-Sandbox-Class from the deployment config."""
    # TODO: derive gpu/gui/headless from num_gpus and image_os.
    return "gui"


class SandboxNextProvider:
    """Provider that talks to the SandboxManager Control HTTP API."""

    def __init__(self, config: RemoteOperatorConfig, *, client: httpx.AsyncClient | None = None):
        self._config = config
        opts = config.provider_options
        self._state_map = opts.get("state_mapping") or _DEFAULT_STATE_MAP
        self._retry_max = opts.get("retry_max", 3)
        self._retry_backoff = opts.get("retry_backoff_base", 0.5)
        self._raw_loader: K8sTemplateLoader | None = None
        if not config.api_key:
            raise ValueError("RemoteOperatorConfig.api_key is required (X-Api-Key header)")

        base_url = config.base_url
        headers: dict[str, str] = {API_KEY_HEADER: config.api_key}
        if config.access_token:
            headers["Authorization"] = f"Bearer {config.access_token}"

        self._client = client or httpx.AsyncClient(
            base_url=base_url,
            timeout=config.default_timeout,
        )
        # Auth / routing headers are provider-level; apply even to an injected client.
        self._client.headers.update(headers)
        logger.info("Initialized SandboxNextProvider (base_url=%s)", config.base_url)

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

        use_raw = config.extended_params.get(EXT_USE_RAW) == EXT_USE_RAW_ENABLED
        if use_raw and not config.template_id:
            body, port_mapping = self._build_raw_request(config)
        else:
            body: dict[str, Any] = {
                "request_id": sandbox_id,
                "resource_spec": {
                    "vcpu_count": int(config.cpus),
                    "memory_mb": parse_size_to_mb(config.memory),
                    "disk_size_mb": parse_size_to_mb(config.disk),
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
            port_mapping = dict(_DEFAULT_PORT_MAPPING)

        sandbox_class = _derive_sandbox_class(config)
        logger.info("[%s] POST /v1/sandboxes body=%s", sandbox_id, body)
        response = await self._request(
            "POST",
            "/v1/sandboxes",
            json=body,
            headers={CLASS_HEADER: sandbox_class},
        )
        response.raise_for_status()
        data = response.json()
        logger.info("[%s] response=%s", sandbox_id, data)

        sn_id = data["sandbox_id"]
        sn_state = data.get("state")
        endpoint = data.get("endpoint") or ""

        logger.info("[%s] sandbox_next submitted, remote_id=%s, state=%s, class=%s", sandbox_id, sn_id, sn_state, sandbox_class)

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
            "port_mapping": port_mapping,
            "extended_params": {
                EXT_BACKEND: BACKEND_NAME,
                EXT_ENDPOINT: endpoint,
            },
        }
        return info

    def _build_raw_request(self, config: DockerDeploymentConfig) -> tuple[dict[str, str], dict[int, int]]:
        """Build the raw-mode request body and port mapping from the configured template."""
        template_name = "default"
        manifest = self._get_raw_template_loader().build_manifest(
            template_name=template_name,
            sandbox_id=config.container_name,
            image=config.image,
            cpus=config.cpus,
            memory=normalize_memory_to_k8s(config.memory),
            disk=normalize_memory_to_k8s(config.disk) if config.disk else None,
            num_gpus=config.num_gpus,
            env_vars=config.env_vars,
        )
        ports = self._config.templates[template_name]["ports"]
        port_mapping = {
            Port.PROXY: ports["proxy"],
            Port.SERVER: ports["server"],
            Port.SSH: ports["ssh"],
        }
        return {"request_id": config.container_name, "raw": json.dumps(manifest)}, port_mapping

    def _get_raw_template_loader(self) -> K8sTemplateLoader:
        """Lazily create the raw-mode manifest loader from RemoteOperatorConfig.templates."""
        if self._raw_loader is None:
            if not self._config.templates:
                raise ValueError("Raw mode requires RemoteOperatorConfig.templates")
            self._raw_loader = K8sTemplateLoader(self._config.templates)
        return self._raw_loader

    async def get_status(self, remote_sandbox_id: str) -> SandboxInfo | None:
        response = await self._request("GET", f"/v1/sandboxes/{remote_sandbox_id}")
        if response.status_code == 404:
            logger.info("[%s] sandbox_next get_status: not found", remote_sandbox_id)
            return None
        response.raise_for_status()
        data = response.json()

        sn_state = data.get("state")
        endpoint = data.get("endpoint") or ""

        # Control-plane RUNNING does not imply rocklet is ready (raw mode downloads
        # it at container start): demote to PENDING until is_alive succeeds.
        state = _map_state(sn_state, self._state_map)
        if state == State.RUNNING and endpoint:
            runtime = self._build_runtime(endpoint)
            try:
                is_alive_response = await runtime.is_alive()
                is_alive = is_alive_response.is_alive
            except Exception as e:
                is_alive = False
            if not is_alive:
                state = State.PENDING
        logger.info("[%s] sandbox_next get_status, state=%s, endpoint=%s", remote_sandbox_id, sn_state, endpoint)

        # Only return fields that change at runtime; static fields are already in redis.
        info: SandboxInfo = {
            "state": state,
            "host_ip": endpoint,
        }
        return info

    def _build_runtime(self, host_ip: str) -> RemoteSandboxRuntime:
        """Build runtime for is_alive probes; proxy port comes from the raw template or the default mapping."""
        proxy_port = _DEFAULT_PORT_MAPPING[Port.PROXY]
        if self._config.templates:
            proxy_port = self._config.templates["default"]["ports"]["proxy"]
        return RemoteSandboxRuntime.from_config(
            RemoteSandboxRuntimeConfig(host=f"http://{host_ip}", port=proxy_port),
        )

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
