"""Unit tests for SandboxNextProvider — mock httpx transport."""

import json
from types import SimpleNamespace

import pytest
import httpx

from rock.actions.sandbox.response import State
from rock.config import RemoteOperatorConfig
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.remote.constants import EXT_ENDPOINT, EXT_BACKEND, EXT_USE_RAW, EXT_USE_RAW_ENABLED, BACKEND_NAME
from rock.sandbox.operator.remote.providers.sandbox_next_provider import (
    SandboxNextProvider,
    _derive_sandbox_class,
    _map_state,
)


# --- Config / fixture helpers ---

def _make_config(**overrides) -> RemoteOperatorConfig:
    defaults = {
        "base_url": "https://api.sandbox.test",
        "api_key": "test-key",
    }
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


class _FakeAliveRuntime:
    """RemoteSandboxRuntime stub for is_alive probes."""

    def __init__(self, alive: bool):
        self._alive = alive

    async def is_alive(self):
        return SimpleNamespace(is_alive=self._alive)


_RAW_TEMPLATES = {
    "default": {
        "ports": {"proxy": 8000, "server": 8080, "ssh": 22},
        "template": {
            "metadata": {"labels": {"example.app": "rock-sandbox"}},
            "spec": {
                "tolerations": [{"operator": "Exists"}],
                "containers": [
                    {
                        "name": "sandbox",
                        "image": "{{ image }}",
                        "resources": {"requests": {"cpu": "{{ cpus }}", "memory": "{{ memory }}"}},
                    }
                ],
            },
        },
    }
}


# --- Utility tests ---

class TestMapState:
    def test_creating(self):
        assert _map_state("SANDBOX_CREATING") == State.PENDING

    def test_allocated(self):
        assert _map_state("SANDBOX_ALLOCATED") == State.PENDING

    def test_running(self):
        assert _map_state("SANDBOX_RUNNING") == State.RUNNING

    def test_pausing(self):
        assert _map_state("SANDBOX_PAUSING") == State.STOPPED

    def test_paused(self):
        assert _map_state("SANDBOX_PAUSED") == State.STOPPED

    def test_deleting(self):
        assert _map_state("SANDBOX_DELETING") == State.STOPPED

    def test_deleted(self):
        assert _map_state("SANDBOX_DELETED") == State.DELETED

    def test_failed(self):
        assert _map_state("SANDBOX_FAILED") == State.STOPPED

    def test_unknown(self):
        assert _map_state("nonsense") == State.PENDING

    def test_none(self):
        assert _map_state(None) == State.PENDING


# --- Provider init tests ---

class TestDeriveSandboxClass:
    def test_defaults_to_gui(self):
        assert _derive_sandbox_class(_make_docker_config()) == "gui"


class TestSandboxNextProviderInit:
    def test_missing_api_key_raises(self):
        with pytest.raises(ValueError, match="api_key is required"):
            SandboxNextProvider(_make_config(api_key=None))

    def test_auth_header_set_on_client(self):
        seen = {"headers": None}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["headers"] = request.headers
            return httpx.Response(200, json={"sandbox_id": "sn-1", "state": "SANDBOX_RUNNING"})

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        provider._client.headers  # headers set at client construction
        assert provider._client.headers["X-Api-Key"] == "test-key"
        assert "X-Sandbox-Class" not in provider._client.headers
        assert "X-Sandbox-Profile-ID" not in provider._client.headers


# --- Provider lifecycle tests ---

class TestSandboxNextProviderSubmit:
    @pytest.mark.asyncio
    async def test_submit_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "POST"
            assert "/v1/sandboxes" in str(request.url)
            assert request.headers["X-Api-Key"] == "test-key"
            assert request.headers["X-Sandbox-Class"] == "gui"
            return httpx.Response(
                201,
                json={
                    "sandbox_id": "sn-abc123",
                    "state": "SANDBOX_CREATING",
                    "endpoint": "",
                },
            )

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        info = await provider.submit(docker_config, {"user_id": "u1", "experiment_id": "e1", "namespace": "ns"})

        assert info["sandbox_id"] == "sb-test-001"
        assert info["state"] == State.PENDING
        assert info["host_ip"] == ""
        assert info["port_mapping"] == {22555: 8000, 8080: 8080, 22: 22}
        ext = info["extended_params"]
        assert info["host_name"] == "sn-abc123"
        assert ext[EXT_BACKEND] == BACKEND_NAME
        assert EXT_ENDPOINT in ext

    @pytest.mark.asyncio
    async def test_submit_builds_resource_spec(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"sandbox_id": "sn-2", "state": "SANDBOX_CREATING"})

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        await provider.submit(docker_config, {})
        assert seen["body"]["resource_spec"] == {"vcpu_count": 2, "memory_mb": 8192, "disk_size_mb": 51200}
        assert seen["body"]["request_id"] == "sb-test-001"

    @pytest.mark.asyncio
    async def test_submit_with_env_vars(self):
        def handler(request: httpx.Request) -> httpx.Response:
            import json

            payload = json.loads(request.content)
            assert payload["env_vars"] == {"FOO": "bar"}
            return httpx.Response(201, json={"sandbox_id": "sn-2", "state": "SANDBOX_CREATING"})

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
            return httpx.Response(201, json={"sandbox_id": "sn-3", "state": "SANDBOX_RUNNING"})

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
            return httpx.Response(201, json={"sandbox_id": "sn-4", "state": "SANDBOX_RUNNING"})

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        docker_config = _make_docker_config()
        await provider.submit(docker_config, {})
        assert "template_id" not in seen["body"]

    @pytest.mark.asyncio
    async def test_submit_no_region_or_class_in_body(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"sandbox_id": "sn-5", "state": "SANDBOX_RUNNING"})

        config = _make_config()
        client = _make_client(handler)
        provider = SandboxNextProvider(config, client=client)
        await provider.submit(_make_docker_config(), {})
        assert "region" not in seen["body"]
        assert "class" not in seen["body"]


class TestSandboxNextProviderSubmitRaw:
    @pytest.mark.asyncio
    async def test_submit_raw_sends_manifest_string(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"sandbox_id": "sn-raw-1", "state": "SANDBOX_CREATING"})

        config = _make_config(templates=_RAW_TEMPLATES)
        provider = SandboxNextProvider(config, client=_make_client(handler))
        info = await provider.submit(_make_docker_config(extended_params={EXT_USE_RAW: EXT_USE_RAW_ENABLED}), {})

        assert set(seen["body"].keys()) == {"request_id", "raw"}
        assert seen["body"]["request_id"] == "sb-test-001"
        assert isinstance(seen["body"]["raw"], str)
        manifest = json.loads(seen["body"]["raw"])
        assert manifest["apiVersion"] == "sandbox.opensandbox.io/v1alpha1"
        assert manifest["kind"] == "BatchSandbox"
        assert manifest["metadata"]["name"] == "sb-test-001"
        assert manifest["metadata"]["labels"]["rock.sandbox/sandbox-id"] == "sb-test-001"
        container = manifest["spec"]["template"]["spec"]["containers"][0]
        assert container["image"] == "python:3.11"
        assert container["resources"]["requests"]["memory"] == "8.00Gi"
        assert info["host_name"] == "sn-raw-1"

    @pytest.mark.asyncio
    async def test_submit_raw_port_mapping_from_template(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(201, json={"sandbox_id": "sn-raw-2", "state": "SANDBOX_RUNNING"})

        templates = {"default": {"ports": {"proxy": 9000, "server": 8081, "ssh": 2222}, "template": {"spec": {"containers": [{"name": "sandbox", "image": "{{ image }}"}]}}}}
        config = _make_config(templates=templates)
        provider = SandboxNextProvider(config, client=_make_client(handler))
        info = await provider.submit(_make_docker_config(extended_params={EXT_USE_RAW: EXT_USE_RAW_ENABLED}), {})
        assert info["port_mapping"] == {22555: 9000, 8080: 8081, 22: 2222}

    @pytest.mark.asyncio
    async def test_submit_raw_merges_env_vars(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"sandbox_id": "sn-raw-3", "state": "SANDBOX_RUNNING"})

        config = _make_config(templates=_RAW_TEMPLATES)
        provider = SandboxNextProvider(config, client=_make_client(handler))
        await provider.submit(_make_docker_config(extended_params={EXT_USE_RAW: EXT_USE_RAW_ENABLED}, env_vars={"FOO": "bar"}), {})
        manifest = json.loads(seen["body"]["raw"])
        env = manifest["spec"]["template"]["spec"]["containers"][0]["env"]
        assert {"name": "FOO", "value": "bar"} in env

    @pytest.mark.asyncio
    async def test_submit_raw_without_templates_raises(self):
        provider = SandboxNextProvider(_make_config(), client=_make_client(lambda r: httpx.Response(201)))
        with pytest.raises(ValueError, match="RemoteOperatorConfig.templates"):
            await provider.submit(_make_docker_config(extended_params={EXT_USE_RAW: EXT_USE_RAW_ENABLED}), {})

    @pytest.mark.asyncio
    async def test_template_id_wins_over_use_raw(self):
        seen = {"body": None}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["body"] = json.loads(request.content)
            return httpx.Response(201, json={"sandbox_id": "sn-tpl", "state": "SANDBOX_RUNNING"})

        config = _make_config(templates=_RAW_TEMPLATES)
        provider = SandboxNextProvider(config, client=_make_client(handler))
        await provider.submit(
            _make_docker_config(template_id="pool-sample", extended_params={EXT_USE_RAW: EXT_USE_RAW_ENABLED}),
            {},
        )
        assert seen["body"]["template_id"] == "pool-sample"
        assert "raw" not in seen["body"]


class TestSandboxNextProviderBuildRuntime:
    def test_default_proxy_port(self):
        provider = SandboxNextProvider(_make_config(), client=_make_client(lambda r: httpx.Response(200)))
        runtime = provider._build_runtime("10.0.0.5")
        assert runtime._config.host == "http://10.0.0.5"
        assert runtime._config.port == 8000

    def test_proxy_port_from_raw_template(self):
        templates = {"default": {"ports": {"proxy": 9000, "server": 8081, "ssh": 2222}, "template": {"spec": {"containers": [{"name": "sandbox", "image": "{{ image }}"}]}}}}
        provider = SandboxNextProvider(_make_config(templates=templates), client=_make_client(lambda r: httpx.Response(200)))
        runtime = provider._build_runtime("10.0.0.5")
        assert runtime._config.port == 9000


class TestSandboxNextProviderGetStatus:
    @pytest.mark.asyncio
    async def test_running(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "sandbox_id": "sn-1",
                "state": "SANDBOX_RUNNING",
                "endpoint": "10.0.0.5",
            })

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        provider._build_runtime = lambda host: _FakeAliveRuntime(True)
        info = await provider.get_status("sn-1")
        assert info is not None
        assert info["state"] == State.RUNNING
        assert info["host_ip"] == "10.0.0.5"

    @pytest.mark.asyncio
    async def test_running_demoted_to_pending_when_rocklet_not_alive(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "sandbox_id": "sn-1",
                "state": "SANDBOX_RUNNING",
                "endpoint": "10.0.0.5",
            })

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        provider._build_runtime = lambda host: _FakeAliveRuntime(False)
        info = await provider.get_status("sn-1")
        assert info is not None
        assert info["state"] == State.PENDING

    @pytest.mark.asyncio
    async def test_running_demoted_to_pending_when_is_alive_raises(self):
        class _RaisingRuntime:
            async def is_alive(self):
                raise RuntimeError("probe failed")

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "sandbox_id": "sn-1",
                "state": "SANDBOX_RUNNING",
                "endpoint": "10.0.0.5",
            })

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        provider._build_runtime = lambda host: _RaisingRuntime()
        info = await provider.get_status("sn-1")
        assert info is not None
        assert info["state"] == State.PENDING

    @pytest.mark.asyncio
    async def test_creating_not_probed(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"sandbox_id": "sn-1", "state": "SANDBOX_CREATING"})

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        provider._build_runtime = lambda host: pytest.fail("is_alive must not be probed before RUNNING")
        info = await provider.get_status("sn-1")
        assert info is not None
        assert info["state"] == State.PENDING

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"code": "not_found", "message": "sandbox not found"})

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        info = await provider.get_status("sn-gone")
        assert info is None


class TestSandboxNextProviderStop:
    @pytest.mark.asyncio
    async def test_stop_delegates_to_delete(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "DELETE"
            return httpx.Response(202, json={"sandbox_id": "sn-1", "state": "SANDBOX_DELETING"})

        provider = SandboxNextProvider(_make_config(), client=_make_client(handler))
        result = await provider.stop("sn-1")
        assert result is True


class TestSandboxNextProviderDelete:
    @pytest.mark.asyncio
    async def test_delete_success(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, json={"sandbox_id": "sn-1", "state": "SANDBOX_DELETING"})

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
