"""Smoke tests for Template API (Warm path).

Core chain: create → get → scale → get → delete.
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


def _wait_ready(client, api_base, template_id):
    """Poll template status until ready or error."""
    status = None
    for _ in range(60):  # timeout ~120s
        resp = client.get(f"{api_base}/templates/{template_id}")
        assert resp.status_code == 200, f"get failed: {resp.text}"
        result = resp.json()["result"]
        assert result["templateID"] == template_id
        status = result["status"]
        if status == "ready":
            return result
        if status == "error":
            pytest.fail(f"template {template_id} error: {result.get('reason')}")
        time.sleep(2)
    pytest.fail(f"template {template_id} not ready after timeout, last status: {status}")


def _wait_capacity(client, api_base, template_id, expected):
    """Poll template status until capacity matches expected or timeout."""
    for _ in range(15):  # timeout ~30s
        resp = client.get(f"{api_base}/templates/{template_id}")
        assert resp.status_code == 200, f"get failed: {resp.text}"
        result = resp.json()["result"]
        assert result["templateID"] == template_id
        capacity_spec = result.get("capacity", {}).get("spec", {})
        if all(capacity_spec.get(k) == v for k, v in expected.items()):
            return result
        time.sleep(2)
    pytest.fail(f"template {template_id} capacity not updated to {expected}, got {capacity_spec}")


def test_template_lifecycle(client, api_base, smoke_image):
    """Full lifecycle: create → get → scale → get → delete."""
    # 1. Create
    template_id = _create_template(client, api_base, smoke_image)

    try:
        # 2. Wait for template ready and check capacity structure
        result = _wait_ready(client, api_base, template_id)
        capacity = result.get("capacity", {})
        assert "spec" in capacity, f"missing capacity.spec: {capacity}"
        assert "status" in capacity, f"missing capacity.status: {capacity}"

        # 3. Scale capacity
        scale_payload = {"poolMin": 2, "poolMax": 5}
        resp = client.post(f"{api_base}/templates/{template_id}/scale", json=scale_payload)
        assert resp.status_code == 200, f"scale failed: {resp.text}"
        scale_result = resp.json()["result"]
        assert scale_result["templateID"] == template_id
        assert scale_result["capacity"]["spec"]["poolMin"] == 2
        assert scale_result["capacity"]["spec"]["poolMax"] == 5

        # 4. Verify scaled capacity via get (with retry for informer cache sync)
        get_result = _wait_capacity(
            client, api_base, template_id, {"poolMin": 2, "poolMax": 5}
        )

        # 5. Delete
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
