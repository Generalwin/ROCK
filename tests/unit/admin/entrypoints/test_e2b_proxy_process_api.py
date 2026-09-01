import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.actions import CommandResponse
from rock.actions.sandbox.response import State
from rock.admin.entrypoints import e2b_proxy_api as e2b_proxy_api_module
from rock.admin.entrypoints.e2b_proxy_api import e2b_proxy_router, set_e2b_proxy_service
from rock.admin.service.e2b_proxy_service import E2BProxyService

_MISSING = object()


@pytest.fixture(autouse=True)
def _restore_e2b_proxy_service():
    previous_service = getattr(e2b_proxy_api_module, "e2b_proxy_service", _MISSING)
    try:
        yield
    finally:
        if previous_service is _MISSING:
            if hasattr(e2b_proxy_api_module, "e2b_proxy_service"):
                delattr(e2b_proxy_api_module, "e2b_proxy_service")
        else:
            set_e2b_proxy_service(previous_service)


@pytest.fixture
def e2b_process_app():
    meta_store = MagicMock()
    meta_store.get = AsyncMock(return_value={"sandbox_id": "sandbox-123", "state": State.RUNNING})
    sandbox_manager = MagicMock()
    sandbox_manager.execute = AsyncMock(return_value=CommandResponse(exit_code=0))
    set_e2b_proxy_service(E2BProxyService(meta_store=meta_store, sandbox_manager=sandbox_manager))

    app = FastAPI()
    app.include_router(e2b_proxy_router)
    return app, meta_store, sandbox_manager


def _connect_envelope(payload: dict) -> bytes:
    encoded = json.dumps(payload, separators=(",", ":")).encode()
    return b"\x00" + len(encoded).to_bytes(4, "big") + encoded


def _decode_envelopes(content: bytes) -> list[tuple[int, dict]]:
    envelopes = []
    offset = 0
    while offset < len(content):
        flags = content[offset]
        size = int.from_bytes(content[offset + 1 : offset + 5], "big")
        start = offset + 5
        end = start + size
        envelopes.append((flags, json.loads(content[start:end])))
        offset = end
    return envelopes


def _request(*, stdin: bool = False) -> dict:
    return {
        "process": {
            "cmd": "/bin/bash",
            "args": ["-l", "-c", "printf test"],
        },
        "stdin": stdin,
    }


async def _start(app: FastAPI, request: dict, *, port: str = "49983"):
    headers = {
        "Connect-Timeout-Ms": "60000",
        "E2b-Sandbox-Id": "sandbox-123",
        "E2b-Sandbox-Port": port,
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/process.Process/Start",
            headers=headers,
            content=_connect_envelope(request),
        )


@pytest.mark.asyncio
async def test_start_rejects_interactive_stdin_without_executing(e2b_process_app):
    app, _, sandbox_manager = e2b_process_app

    response = await _start(app, _request(stdin=True))

    assert _decode_envelopes(response.content) == [
        (
            2,
            {
                "error": {
                    "code": "unimplemented",
                    "message": "interactive stdin is not supported",
                }
            },
        )
    ]
    sandbox_manager.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_does_not_require_the_e2b_envd_port(e2b_process_app):
    app, _, sandbox_manager = e2b_process_app

    response = await _start(app, _request(), port="1234")

    envelopes = _decode_envelopes(response.content)
    assert envelopes[-1] == (2, {})
    sandbox_manager.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_start_does_not_execute_for_a_stopped_sandbox(e2b_process_app):
    app, meta_store, sandbox_manager = e2b_process_app
    meta_store.get.return_value = {"sandbox_id": "sandbox-123", "state": State.STOPPED}

    response = await _start(app, _request())

    assert _decode_envelopes(response.content) == [
        (
            2,
            {
                "error": {
                    "code": "not_found",
                    "message": "sandbox is not running",
                }
            },
        )
    ]
    sandbox_manager.execute.assert_not_awaited()
