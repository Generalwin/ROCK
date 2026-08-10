"""Unit tests for BatchSandboxProvider template (Pool warm path) methods.

Tests the operator/provider layer directly:
- generate_template_id
- TemplateSpec dataclass
- _map_pool_to_template_status
- scale_template
- K8sConstants for Pool CRD
"""

import pytest

from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.provider import BatchSandboxProvider, TemplateSpec, generate_template_id
from rock.sdk.common.exceptions import BadRequestRockError


class TestGenerateTemplateId:
    """Tests for generate_template_id function."""

    def test_same_spec_same_id(self):
        """Same (fromImage, cpuCount, memoryMB) produce the same template ID."""
        spec1 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        spec2 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        assert generate_template_id(spec1) == generate_template_id(spec2)

    def test_different_image_different_id(self):
        """Different fromImage produces different template IDs."""
        spec1 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        spec2 = TemplateSpec(from_image="python:3.12", cpu_count=2, memory_mb=2048)
        assert generate_template_id(spec1) != generate_template_id(spec2)

    def test_different_cpu_different_id(self):
        """Different cpuCount produces different template IDs."""
        spec1 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        spec2 = TemplateSpec(from_image="python:3.11", cpu_count=4, memory_mb=2048)
        assert generate_template_id(spec1) != generate_template_id(spec2)

    def test_different_memory_different_id(self):
        """Different memoryMB produces different template IDs."""
        spec1 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        spec2 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=4096)
        assert generate_template_id(spec1) != generate_template_id(spec2)

    def test_optional_fields_affect_id(self):
        """All non-null optional fields participate in template ID."""
        base = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        with_disk = TemplateSpec(
            from_image="python:3.11", cpu_count=2, memory_mb=2048, disk_gb=40,
        )
        with_gpu = TemplateSpec(
            from_image="python:3.11", cpu_count=2, memory_mb=2048, num_gpus=2,
        )
        with_accelerator = TemplateSpec(
            from_image="python:3.11", cpu_count=2, memory_mb=2048, accelerator_type="A100",
        )
        with_os = TemplateSpec(
            from_image="python:3.11", cpu_count=2, memory_mb=2048, os="linux",
        )
        assert generate_template_id(base) != generate_template_id(with_disk)
        assert generate_template_id(base) != generate_template_id(with_gpu)
        assert generate_template_id(base) != generate_template_id(with_accelerator)
        assert generate_template_id(base) != generate_template_id(with_os)

    def test_different_os_different_id(self):
        """Different os produces different template IDs."""
        spec1 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048, os="linux")
        spec2 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048, os="windows")
        assert generate_template_id(spec1) != generate_template_id(spec2)

    def test_id_has_prefix(self):
        """Template ID starts with the configured prefix."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        tid = generate_template_id(spec)
        assert tid.startswith(K8sConstants.TEMPLATE_ID_PREFIX)


class TestTemplateSpec:
    """Tests for TemplateSpec dataclass."""

    def test_required_fields(self):
        """Required fields are set correctly."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        assert spec.from_image == "python:3.11"
        assert spec.cpu_count == 2
        assert spec.memory_mb == 2048

    def test_optional_fields_default_none(self):
        """Optional fields default to None."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        assert spec.disk_gb is None
        assert spec.num_gpus is None
        assert spec.accelerator_type is None
        assert spec.os is None

    def test_capacity_fields_removed(self):
        """Capacity fields are excluded from TemplateSpec identity."""
        with pytest.raises(TypeError):
            TemplateSpec(
                from_image="python:3.11",
                cpu_count=2,
                memory_mb=2048,
                buffer_min=1,
            )


def _make_provider():
    """Create a BatchSandboxProvider without calling __init__."""
    return object.__new__(BatchSandboxProvider)


class TestMapPoolToTemplateStatus:
    """Tests for Pool CRD to template status mapping (design doc format)."""

    def test_ready_pool(self):
        """Pool with available > 0 maps to 'ready'."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "spec": {
                "capacitySpec": {"poolMin": 1, "poolMax": 10, "bufferMin": 1, "bufferMax": 3},
            },
            "status": {"available": 2, "total": 3, "allocated": 1},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["template_id"] == "tpl-abc123"
        assert result["status"] == "ready"
        assert result["reason"] is None
        assert result["created_at"] == "2026-08-03T06:00:00Z"
        assert result["capacity"]["spec"]["pool_min"] == 1
        assert result["capacity"]["status"]["available"] == 2

    def test_building_pool(self):
        """Pool with total but no available maps to 'building'."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "spec": {
                "capacitySpec": {"poolMin": 1, "poolMax": 10, "bufferMin": 1, "bufferMax": 3},
            },
            "status": {"available": 0, "total": 3, "allocated": 0},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["status"] == "building"
        assert result["capacity"]["spec"]["pool_max"] == 10
        assert result["capacity"]["status"]["total"] == 3

    def test_new_pool_no_status(self):
        """Pool with no status section maps to 'building'."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "spec": {
                "capacitySpec": {"poolMin": 1, "poolMax": 10, "bufferMin": 1, "bufferMax": 3},
            },
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["status"] == "building"
        assert result["reason"] is None
        assert result["capacity"]["spec"]["buffer_min"] == 1
        assert result["capacity"]["status"]["available"] is None

    def test_zero_capacity_pool_ready(self):
        """Pool with poolMin=0 and bufferMin=0 maps to 'ready' without replicas."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "spec": {
                "capacitySpec": {"poolMin": 0, "bufferMin": 0, "poolMax": 0, "bufferMax": 0},
            },
            "status": {"available": 0, "total": 0},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["status"] == "ready"
        assert result["reason"] is None
        assert result["capacity"]["spec"]["pool_min"] == 0
        assert result["capacity"]["status"]["total"] == 0

    def test_updated_at_fallback(self):
        """updated_at falls back to creationTimestamp when status has no timestamp."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "spec": {
                "capacitySpec": {"poolMin": 1, "poolMax": 10, "bufferMin": 1, "bufferMax": 3},
            },
            "status": {"available": 2, "total": 3},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["updated_at"] == "2026-08-03T06:00:00Z"


@pytest.fixture
def anyio_backend():
    """Run anyio async tests on asyncio backend only."""
    return "asyncio"


class MockPoolApi:
    """Minimal mock for provider scale tests."""

    def __init__(self):
        self.existing_pool = None
        self.last_patch = None

    async def get_custom_object(self, name: str):
        return self.existing_pool

    async def update_custom_object(self, name: str, body: dict):
        self.last_patch = body
        return self.existing_pool


class TestScaleTemplate:
    """Tests for BatchSandboxProvider.scale_template."""

    def _make_provider(self):
        """Create a provider with mocked _pool_api and initialized flag."""
        provider = object.__new__(BatchSandboxProvider)
        provider._initialized = True
        provider._pool_api = MockPoolApi()
        return provider

    @pytest.mark.anyio
    async def test_scale_updates_capacity(self):
        """Scale patches capacitySpec and returns mapped status."""
        provider = self._make_provider()
        provider._pool_api.existing_pool = {
            "metadata": {"name": "tpl-abc", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "spec": {"capacitySpec": {"poolMin": 1, "poolMax": 10, "bufferMin": 1, "bufferMax": 3}},
            "status": {"available": 2, "total": 3},
        }

        result = await provider.scale_template("tpl-abc", {"pool_min": 2, "pool_max": 20})

        assert result["template_id"] == "tpl-abc"
        assert result["status"] == "ready"
        assert result["capacity"]["spec"]["pool_min"] == 1
        assert result["capacity"]["status"]["available"] == 2
        assert provider._pool_api.last_patch == {
            "spec": {"capacitySpec": {"poolMin": 2, "poolMax": 20}}
        }

    @pytest.mark.anyio
    async def test_scale_not_found(self):
        """Scaling a non-existent template raises BadRequestRockError."""
        provider = self._make_provider()
        provider._pool_api.existing_pool = None

        with pytest.raises(BadRequestRockError, match="Template tpl-missing not found"):
            await provider.scale_template("tpl-missing", {"pool_min": 1})

    @pytest.mark.anyio
    async def test_scale_empty_capacity(self):
        """Empty capacity dict raises BadRequestRockError."""
        provider = self._make_provider()

        with pytest.raises(BadRequestRockError, match="No capacity fields provided"):
            await provider.scale_template("tpl-abc", {})

    @pytest.mark.anyio
    async def test_scale_invalid_field(self):
        """Invalid capacity field raises BadRequestRockError."""
        provider = self._make_provider()

        with pytest.raises(BadRequestRockError, match="Invalid capacity fields"):
            await provider.scale_template("tpl-abc", {"pool_min": 1, "unknown": 5})


class TestK8sConstants:
    """Tests for new K8sConstants entries."""

    def test_pool_crd_constants(self):
        """Pool CRD constants are set correctly."""
        assert K8sConstants.CRD_PLURAL_POOL == "pools"
        assert K8sConstants.CRD_KIND_POOL == "Pool"

    def test_label_constants(self):
        """Label constants are set correctly."""
        assert K8sConstants.LABEL_MANAGED_BY == "rock.sandbox/managed-by"
        assert K8sConstants.LABEL_MANAGED_BY_TEMPLATE_API == "template-api"

    def test_template_id_prefix(self):
        """Template ID prefix is set correctly."""
        assert K8sConstants.TEMPLATE_ID_PREFIX == "tpl-"
