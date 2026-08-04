"""Unit tests for Template API (Warm path) implementation."""
import copy

import jinja2
import pytest

from rock.sandbox.operator.k8s.constants import K8sConstants
from rock.sandbox.operator.k8s.provider import BatchSandboxProvider, TemplateSpec, generate_template_id
from rock.utils.jinja_render import render_node


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

    def test_placeholder_fields_ignored(self):
        """Phase 1: diskGB, numGpus, acceleratorType don't affect template ID."""
        spec1 = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        spec2 = TemplateSpec(
            from_image="python:3.11", cpu_count=2, memory_mb=2048,
            disk_gb=40, num_gpus=2, accelerator_type="A100",
        )
        assert generate_template_id(spec1) == generate_template_id(spec2)

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


class TestBuildPoolManifest:
    """Tests for Pool CRD manifest building (Jinja2 rendering)."""

    POOL_TEMPLATE = {
        "capacitySpec": {
            "bufferMin": "{{ buffer_min | default(1) }}",
            "bufferMax": "{{ buffer_max | default(3) }}",
            "poolMin": "{{ pool_min | default(1) }}",
            "poolMax": "{{ pool_max | default(10) }}",
        },
        "template": {
            "metadata": {"labels": {"app": "rock-pool"}},
            "spec": {
                "tolerations": [{"operator": "Exists"}],
                "containers": [{
                    "name": "main",
                    "image": "{{ from_image }}",
                    "resources": {
                        "limits": {
                            "cpu": "{{ cpu_count }}",
                            "memory": "{{ memory_mb }}Mi",
                        },
                        "requests": {
                            "cpu": "{{ cpu_count }}",
                            "memory": "{{ memory_mb }}Mi",
                        },
                    },
                }],
            },
        },
    }

    def test_jinja_render_image(self):
        """Jinja2 renders from_image variable."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        ctx = {
            "from_image": spec.from_image,
            "cpu_count": spec.cpu_count,
            "memory_mb": spec.memory_mb,
        }
        rendered = render_node(copy.deepcopy(self.POOL_TEMPLATE), jinja2.Environment(), ctx)
        assert rendered["template"]["spec"]["containers"][0]["image"] == "python:3.11"

    def test_jinja_render_cpu(self):
        """Jinja2 renders cpu_count variable."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=4, memory_mb=2048)
        ctx = {
            "from_image": spec.from_image,
            "cpu_count": spec.cpu_count,
            "memory_mb": spec.memory_mb,
        }
        rendered = render_node(copy.deepcopy(self.POOL_TEMPLATE), jinja2.Environment(), ctx)
        assert rendered["template"]["spec"]["containers"][0]["resources"]["limits"]["cpu"] == "4"

    def test_jinja_render_memory(self):
        """Jinja2 renders memory_mb variable."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=4096)
        ctx = {
            "from_image": spec.from_image,
            "cpu_count": spec.cpu_count,
            "memory_mb": spec.memory_mb,
        }
        rendered = render_node(copy.deepcopy(self.POOL_TEMPLATE), jinja2.Environment(), ctx)
        assert rendered["template"]["spec"]["containers"][0]["resources"]["limits"]["memory"] == "4096Mi"

    def test_jinja_render_capacity_default(self):
        """Capacity uses defaults when not specified."""
        spec = TemplateSpec(from_image="python:3.11", cpu_count=2, memory_mb=2048)
        ctx = {
            "from_image": spec.from_image,
            "cpu_count": spec.cpu_count,
            "memory_mb": spec.memory_mb,
        }
        rendered = render_node(copy.deepcopy(self.POOL_TEMPLATE), jinja2.Environment(), ctx)
        assert rendered["capacitySpec"]["bufferMin"] == "1"
        assert rendered["capacitySpec"]["bufferMax"] == "3"
        assert rendered["capacitySpec"]["poolMin"] == "1"
        assert rendered["capacitySpec"]["poolMax"] == "10"


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
            "status": {"available": 2, "total": 3},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["template_id"] == "tpl-abc123"
        assert result["status"] == "ready"
        assert result["reason"] is None
        assert result["created_at"] == "2026-08-03T06:00:00Z"

    def test_building_pool(self):
        """Pool with total but no available maps to 'building'."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "status": {"available": 0, "total": 3},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["status"] == "building"

    def test_new_pool_no_status(self):
        """Pool with no status section maps to 'building'."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["status"] == "building"
        assert result["reason"] is None

    def test_error_pool(self):
        """Pool with Ready=False condition maps to 'error' with reason."""
        provider = _make_provider()
        mock_pool = {
            "metadata": {"name": "tpl-abc123", "creationTimestamp": "2026-08-03T06:00:00Z"},
            "status": {
                "available": 0,
                "total": 0,
                "conditions": [{"type": "Ready", "status": "False", "message": "insufficient resources"}],
            },
        }
        result = provider._map_pool_to_template_status(mock_pool)
        assert result["status"] == "error"
        assert result["reason"] is not None
        assert result["reason"]["message"] == "insufficient resources"
        assert result["reason"]["step"] == "create_fiber_pool"


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
