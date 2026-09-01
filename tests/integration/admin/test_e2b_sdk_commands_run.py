import shlex
import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn
from e2b import CommandExitException, Sandbox, TimeoutException
from fastapi import FastAPI, Request
from httpx import AsyncClient

from rock.actions.sandbox.response import State
from rock.admin.entrypoints import e2b_api as e2b_api_module
from rock.admin.entrypoints import e2b_proxy_api as e2b_proxy_api_module
from rock.admin.entrypoints.e2b_api import e2b_router, set_e2b_service
from rock.admin.entrypoints.e2b_proxy_api import e2b_proxy_router, set_e2b_proxy_service
from rock.admin.proto.response import SandboxStartResponse
from rock.admin.service.e2b_proxy_service import E2BProxyService
from rock.deployments.constants import Port
from rock.sandbox.service.sandbox_proxy_service import SandboxProxyService

_MISSING = object()
pytestmark = [pytest.mark.integration]


@contextmanager
def _serve_tcp(app: FastAPI):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            log_level="error",
            access_log=False,
            lifespan="off",
        )
    )
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 5
    while not server.started and thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()
        raise RuntimeError("test ASGI server did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


class _HTTPClient:
    async def request(self, **kwargs):
        async with AsyncClient() as client:
            return await client.request(**kwargs)


@pytest.fixture
def e2b_command_stack(rocklet_remote_server):
    api_key = "e2b_0000000000000000000000000000000000000000"
    control_headers = []
    data_headers = []

    control_service = MagicMock()
    control_service.start = AsyncMock(
        return_value=SandboxStartResponse(
            sandbox_id="sandbox-123",
            host_name="sandbox-123",
            host_ip="127.0.0.1",
        )
    )
    control_app = FastAPI()

    @control_app.middleware("http")
    async def capture_control_api_key(request: Request, call_next):
        control_headers.append((request.url.path, request.headers.get("x-api-key")))
        return await call_next(request)

    control_app.include_router(e2b_router)

    sandbox_record = {
        "sandbox_id": "sandbox-123",
        "state": State.RUNNING,
        "host_ip": "127.0.0.1",
        "port_mapping": {int(Port.PROXY): rocklet_remote_server.port},
        "env": {"BASE": "sandbox"},
    }
    meta_store = MagicMock()
    meta_store.get = AsyncMock(return_value=sandbox_record)
    meta_store.get_timeout = AsyncMock(return_value=None)

    sandbox_manager = SandboxProxyService.__new__(SandboxProxyService)
    sandbox_manager.metrics_monitor = None
    sandbox_manager._meta_store = meta_store
    sandbox_manager._rpc_client = _HTTPClient()

    proxy_service = E2BProxyService(
        meta_store=meta_store,
        sandbox_manager=sandbox_manager,
    )
    data_app = FastAPI()

    @data_app.middleware("http")
    async def capture_data_access_token(request: Request, call_next):
        data_headers.append((request.url.path, request.headers.get("x-access-token")))
        return await call_next(request)

    data_app.include_router(e2b_proxy_router)

    previous_control_service = getattr(e2b_api_module, "e2b_service", _MISSING)
    previous_proxy_service = getattr(e2b_proxy_api_module, "e2b_proxy_service", _MISSING)
    try:
        set_e2b_service(control_service)
        set_e2b_proxy_service(proxy_service)
        with _serve_tcp(control_app) as api_url, _serve_tcp(data_app) as sandbox_url:
            yield {
                "api_url": api_url,
                "sandbox_url": sandbox_url,
                "api_key": api_key,
                "control_headers": control_headers,
                "data_headers": data_headers,
            }
    finally:
        if previous_control_service is _MISSING:
            delattr(e2b_api_module, "e2b_service")
        else:
            set_e2b_service(previous_control_service)
        if previous_proxy_service is _MISSING:
            delattr(e2b_proxy_api_module, "e2b_proxy_service")
        else:
            set_e2b_proxy_service(previous_proxy_service)


def _create_sandbox(stack) -> Sandbox:
    return Sandbox.create(
        template="linux-dind",
        timeout=60,
        api_url=stack["api_url"],
        sandbox_url=stack["sandbox_url"],
        api_key=stack["api_key"],
    )


def test_commands_run_success_end_to_end(e2b_command_stack, tmp_path):
    sandbox = _create_sandbox(e2b_command_stack)
    result = sandbox.commands.run(
        'printf "%s:%s\\n" "$BASE" "$REQUEST"; pwd; printf "warning\\n" >&2',
        stdin=False,
        envs={"REQUEST": "request"},
        cwd=str(tmp_path),
        timeout=2.5,
    )

    stdout_lines = result.stdout.splitlines()
    assert stdout_lines[0] == "sandbox:request"
    assert Path(stdout_lines[1]).resolve() == tmp_path.resolve()
    assert result.stderr == "warning\n"
    assert result.exit_code == 0
    assert ("/sandboxes", e2b_command_stack["api_key"]) in e2b_command_stack["control_headers"]
    assert ("/process.Process/Start", e2b_command_stack["api_key"]) in e2b_command_stack["data_headers"]


def test_commands_run_nonzero_exit_end_to_end(e2b_command_stack):
    sandbox = _create_sandbox(e2b_command_stack)

    with pytest.raises(CommandExitException) as error:
        sandbox.commands.run(
            "printf 'before exit\\n'; printf 'failure\\n' >&2; exit 23",
            stdin=False,
            timeout=2.5,
        )

    assert error.value.stdout == "before exit\n"
    assert error.value.stderr == "failure\n"
    assert error.value.exit_code == 23
    assert error.value.error == "exit status 23"


def test_commands_run_background_end_to_end(e2b_command_stack, tmp_path):
    sandbox = _create_sandbox(e2b_command_stack)
    release_file = tmp_path / "release-command"
    command = f"while [ ! -f {shlex.quote(str(release_file))} ]; do sleep 0.01; done; printf background"

    handle = sandbox.commands.run(
        command,
        background=True,
        stdin=False,
        timeout=2.5,
    )
    assert not release_file.exists()
    release_file.touch()
    result = handle.wait()

    assert handle.pid > 0
    assert result.stdout == "background"
    assert result.stderr == ""
    assert result.exit_code == 0


def test_commands_run_timeout_end_to_end(e2b_command_stack):
    sandbox = _create_sandbox(e2b_command_stack)

    with pytest.raises(TimeoutException, match="command timed out"):
        sandbox.commands.run(
            "sleep 1",
            stdin=False,
            timeout=0.01,
        )
