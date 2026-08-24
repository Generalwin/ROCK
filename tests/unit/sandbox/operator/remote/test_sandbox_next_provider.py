"""Unit tests for SandboxNextProvider — mock httpx transport."""

import pytest
import httpx

from rock.actions.sandbox.response import State
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.remote.constants import EXT_ENDPOINT, EXT_BACKEND, BACKEND_NAME
from rock.sandbox.operator.remote.providers.sandbox_next_provider import (
    SandboxNextProvider,
    _map_state,
    _parse_mem_to_mb,
    _parse_disk_to_mb,
)


# --- Config / fixture helpers ---

def _make_config(**overrides) -> RemoteOperatorConfig:
    defaults = {"base_url": "https://api.sandbox.test", "api_key": "test-key"}
    defaults.update(overrides)
    return RemoteOperatorConfig(**defaults)


def _make_docker_config(**overrides) -> DockerDeploymentConfig:
    defaults = {
        "image": "python:3.11",
        "cpus": 2.0,
        "memory": "8g",
        "disk": "50G",
        "container_name": "sb-test-001",
    }
    defaults.update(overrides)
    return DockerDeploymentConfig(**defaults)


def _make_client(handler) -> httpx.AsyncClient:
    """Create an httpx.AsyncClient with a mock transport."""
    return httpx.AsyncClient(
        base_url="https://api.sandbox.test",
        transport=httpx.MockTransport(handler),
    )


# --- Utility tests ---

class TestParseMemToMb:
    def test_gigabytes(self):
        assert _parse_mem_to_mb("8g") == 8192

    def test_megabytes(self):
        assert _parse_mem_to_mb("4096m") == 4096

    def test_plain_number(self):
        assert _parse_mem_to_mb("2048") == 2048

    def test_empty(self):
        assert _parse_mem_to_mb("") == 0

    def test_uppercase(self):
        assert _parse_mem_to_mb("4G") == 4096


class TestParseDiskToMb:
    def test_gigabytes(self):
        assert _parse_disk_to_mb("50G") == 51200

    def test_none(self):
        assert _parse_disk_to_mb(None) == 0


class TestMapState:
    def test_creating(self):
        assert _map_state("creating") == State.PENDING

    def test_running(self):
        assert _map_state("running") == State.RUNNING

    def test_paused(self):
        assert _map_state("paused") == State.STOPPED

    def test_failed(self):
        assert _map_state("failed") == State.STOPPED

    def test_unknown(self):
        assert _map_state("nonsense") == State.PENDING

    def test_none(self):
        assert _map_state(None) == State.PENDING


# --- Provider lifecycle tests ---

class TestSandboxNextProviderSubmit:
    @pytest.mark.asyncio
    async def test_submit_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert "/v1/sandboxes" in str(request.url)
            body = httpx.Response(
                201,
                json={
                    "sandbox_id": "sn-abc123",
                    "region": "cn-hangzhou",
                    "class": "headless-vm",
                    "state": "creating",
                    "access": {
                        "endpoint_template": "10.0.0.1",
                        "agent_token": "agent-token-xyz",
                    },
                },
            )
            return body

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        info = await provider.submit(docker_config, {"user_id": "u1", "experiment_id": "e1", "namespace": "ns"})

        assert info["sandbox_id"] == "sb-test-001"
        assert info["state"] == State.PENDING
        assert info["auth_token"] == "agent-token-xyz"
        assert info["host_ip"] == "10.0.0.1"
        assert info["port_mapping"] == {22555: 8000, 8080: 8080, 22: 22}
        ext = info["extended_params"]
        assert info["host_name"] == "sn-abc123"
        assert ext[EXT_BACKEND] == BACKEND_NAME
        assert EXT_ENDPOINT in ext

    @pytest.mark.asyncio
    async def test_submit_with_env_vars(self):
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            payload = json.loads(request.content)
            assert payload["env_vars"] == {"FOO": "bar"}
            return httpx.Response(202, json={
                "sandbox_id": "sn-2",
                "state": "creating",
                "access": {"endpoint_template": "10.0.0.2", "agent_token": ""},
            })

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config(env_vars={"FOO": "bar"})
        info = await provider.submit(docker_config, {})
        assert info["host_name"] == "sn-2"

    @pytest.mark.asyncio
    async def test_submit_with_template_id(self):
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            payload = json.loads(request.content)
            assert payload["template_id"] == "pool-default"
            return httpx.Response(201, json={
                "sandbox_id": "sn-3",
                "state": "running",
                "access": {"endpoint_template": "10.0.0.1", "agent_token": "tok"},
            })

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config(template_id="pool-default")
        info = await provider.submit(docker_config, {})
        assert info["host_name"] == "sn-3"

    @pytest.mark.asyncio
    async def test_submit_without_template_id_omits_field(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={
                "sandbox_id": "sn-4",
                "state": "running",
                "access": {"endpoint_template": "10.0.0.2", "agent_token": ""},
            })

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        await provider.submit(docker_config, {})
        assert "template_id" not in seen["body"]

    @pytest.mark.asyncio
    async def test_submit_uses_provider_options_for_region_and_class(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={
                "sandbox_id": "sn-5",
                "state": "running",
                "access": {"endpoint_template": "10.0.0.3", "agent_token": ""},
            })

        config = _make_config(provider_options={"region": "cn-wulanchabu", "sandbox_class": "gui"})
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        await provider.submit(docker_config, {})
        assert seen["body"]["region"] == "cn-wulanchabu"
        assert seen["body"]["class"] == "gui"

    @pytest.mark.asyncio
    async def test_submit_uses_defaults_without_provider_options(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={
                "sandbox_id": "sn-6",
                "state": "running",
                "access": {"endpoint_template": "10.0.0.4", "agent_token": ""},
            })

        config = _make_config()  # no provider_options
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        await provider.submit(docker_config, {})
        assert seen["body"]["region"] == "cn-hangzhou"
        assert seen["body"]["class"] == "headless-vm"


class TestSandboxNextProviderGetStatus:
    @pytest.mark.asyncio
    async def test_running(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "sandbox_id": "sn-1",
                "state": "running",
                "access": {"endpoint_template": "10.0.0.5", "agent_token": "tok"},
            })

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        info = await provider.get_status("sn-1")
        assert info is not None
        assert info["state"] == State.RUNNING
        assert info["auth_token"] == "tok"

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        info = await provider.get_status("sn-gone")
        assert info is None


class TestSandboxNextProviderStop:
    @pytest.mark.asyncio
    async def test_stop_delegates_to_delete(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            return httpx.Response(202)

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        result = await provider.stop("sn-1")
        assert result is True


class TestSandboxNextProviderDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202)

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        assert await provider.delete("sn-1") is True

    @pytest.mark.asyncio
    async def test_delete_404(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404)

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        assert await provider.delete("sn-gone") is True


# --- Template API tests (currently unsupported) ---

class TestSandboxNextProviderTemplate:
    @pytest.mark.asyncio
    async def test_create_template_not_implemented(self):
        provider = SandboxNextProvider(_make_config(), client=_make_client(lambda r: httpx.Response(200)))
        with pytest.raises(NotImplementedError):
            await provider.create_template({"template_id": "tpl-1"})

    @pytest.mark.asyncio
    async def test_get_template_status_not_implemented(self):
        provider = SandboxNextProvider(_make_config(), client=_make_client(lambda r: httpx.Response(200)))
        with pytest.raises(NotImplementedError):
            await provider.get_template_status("tpl-1")

    @pytest.mark.asyncio
    async def test_delete_template_not_implemented(self):
        provider = SandboxNextProvider(_make_config(), client=_make_client(lambda r: httpx.Response(200)))
        with pytest.raises(NotImplementedError):
            await provider.delete_template("tpl-1")
