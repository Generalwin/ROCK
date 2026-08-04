"""Smoke tests for Template API (Warm path).

Core chain: create → get → delete.
Run: pytest tests/smoke/test_template.py --admin-url http://localhost:8080 --smoke-image python:3.11
"""

import time

import pytest

pytestmark = pytest.mark.need_admin


def _create_template(client, api_base, image, **overrides):
    payload = {"fromImage": image, "cpuCount": 2, "memoryMB": 2048}
    payload.update(overrides)
    resp = client.post(f"{api_base}/templates", json=payload)
    assert resp.status_code == 200, f"create failed: {resp.text}"
    data = resp.json()
    assert data["status"] == "Success", f"create error: {data}"
    result = data["result"]
    assert result["templateID"].startswith("tpl-")
    return result["templateID"]


def test_template_lifecycle(client, api_base, smoke_image):
    """Full lifecycle: create → get → delete."""
    # 1. Create
    template_id = _create_template(client, api_base, smoke_image)

    try:
        # 2. Wait for template ready
        for _ in range(60):  # timeout ~120s
            resp = client.get(f"{api_base}/templates/{template_id}")
            assert resp.status_code == 200, f"get failed: {resp.text}"
            result = resp.json()["result"]
            assert result["templateID"] == template_id
            status = result["status"]
            if status == "ready":
                break
            if status == "error":
                pytest.fail(f"template {template_id} error: {result.get('reason')}")
            time.sleep(2)
        else:
            pytest.fail(f"template {template_id} not ready after timeout, last status: {status}")

        # 3. Delete
        resp = client.delete(f"{api_base}/templates/{template_id}")
        assert resp.status_code == 200, f"delete failed: {resp.text}"
        assert "deleted" in resp.json()["result"]
    finally:
        # Always cleanup, even on failure
        client.delete(f"{api_base}/templates/{template_id}")


def test_template_idempotent_create(client, api_base, smoke_image):
    """Same spec produces the same template ID."""
    tid1 = _create_template(client, api_base, smoke_image)
    try:
        tid2 = _create_template(client, api_base, smoke_image)
        assert tid1 == tid2
    finally:
        client.delete(f"{api_base}/templates/{tid1}")
