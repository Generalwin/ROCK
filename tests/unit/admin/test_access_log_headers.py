import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from rock.admin import main as admin_main


@pytest.mark.asyncio
async def test_access_log_records_request_headers_without_transformation(monkeypatch):
    access_logger = MagicMock()
    monkeypatch.setattr(admin_main, "init_logger", lambda *_args, **_kwargs: access_logger)
    app = FastAPI()
    app.middleware("http")(admin_main.log_requests_and_responses)

    @app.get("/ok")
    async def ok():
        return {"ok": True}

    headers = {
        "X-API-Key": "control-secret",
        "X-Access-Token": "data-secret",
        "Authorization": "Basic user-secret",
        "X-Key": "legacy-secret",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/ok", headers=headers)

    assert response.status_code == 200
    request_log = json.loads(access_logger.info.call_args_list[0].args[0])
    logged_headers = request_log["headers"]
    assert logged_headers["x-api-key"] == "control-secret"
    assert logged_headers["x-access-token"] == "data-secret"
    assert logged_headers["authorization"] == "Basic user-secret"
    assert logged_headers["x-key"] == "legacy-secret"
