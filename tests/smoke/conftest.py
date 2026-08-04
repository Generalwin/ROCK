"""Smoke test configuration.

Usage:
    pytest tests/smoke/ --admin-url http://localhost:8080 --smoke-image python:3.11
    pytest tests/smoke/  # defaults to http://localhost:8080, image=python:3.11
"""

import httpx
import pytest

API_PREFIX = "/apis/envs/sandbox/v1"


def pytest_addoption(parser):
    parser.addoption(
        "--admin-url",
        default="http://localhost:8080",
        help="Admin service base URL (default: http://localhost:8080)",
    )
    parser.addoption(
        "--smoke-image",
        default="python:3.11",
        help="Docker image for smoke tests (default: python:3.11)",
    )


@pytest.fixture(scope="session")
def admin_url(request):
    return request.config.getoption("--admin-url").rstrip("/")


@pytest.fixture(scope="session")
def smoke_image(request):
    return request.config.getoption("--smoke-image")


@pytest.fixture(scope="session")
def api_base(admin_url):
    return f"{admin_url}{API_PREFIX}"


@pytest.fixture
def client():
    with httpx.Client(timeout=30) as c:
        yield c
