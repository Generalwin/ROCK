"""Unit tests for E2BService template resolution (raw-mode decision)."""

from unittest.mock import AsyncMock

import pytest

from rock.admin.service.e2b_service import E2BService
from rock.deployments.config import DockerDeploymentConfig
from rock.sandbox.operator.remote.constants import EXT_USE_RAW, EXT_USE_RAW_ENABLED


def _make_service(ready_template):
    template_table = AsyncMock()
    template_table.get_ready_template.return_value = ready_template
    sandbox_manager = AsyncMock()
    return E2BService(sandbox_manager, template_table), sandbox_manager


class TestE2BServiceStart:
    @pytest.mark.asyncio
    async def test_no_ready_template_switches_to_raw(self):
        service, manager = _make_service(None)
        config = DockerDeploymentConfig(image="my-img:1", template_id="my-img:1")

        await service.start(config)

        passed = manager.start_from_template.call_args[0][0]
        assert passed.extended_params.get(EXT_USE_RAW) == EXT_USE_RAW_ENABLED
        assert passed.template_id is None
        assert passed.image == "my-img:1"

    @pytest.mark.asyncio
    async def test_ready_template_overrides_resources(self):
        ready = {"image": "reg/img:1", "cpu_count": 4, "memory_mb": 8192, "disk_size_mb": 51200}
        service, manager = _make_service(ready)
        config = DockerDeploymentConfig(image="pool-x", template_id="pool-x")

        await service.start(config)

        passed = manager.start_from_template.call_args[0][0]
        assert EXT_USE_RAW not in passed.extended_params
        assert passed.template_id == "pool-x"
        assert passed.image == "reg/img:1"
        assert passed.cpus == 4
        assert passed.memory == "8g"
        assert passed.disk == "50g"
