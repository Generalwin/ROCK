"""Template API (Warm path) — create / query / delete Template.

POST    /internal/v1/templates           — create or reuse template
GET     /internal/v1/templates/{id}      — query template status
DELETE  /internal/v1/templates/{id}      — delete template

All calls go through sandbox_manager, consistent with the sandbox API pattern.
"""

from fastapi import APIRouter

from rock.actions import RockResponse
from rock.admin.entrypoints import sandbox_api
from rock.admin.proto.request import TemplateCreateRequest, TemplateScaleRequest
from rock.admin.proto.response import TemplateCreateResponse, TemplateStatusResponse
from rock.common.exception import handle_exceptions
from rock.common.validation import NonBlankStr
from rock.logger import init_logger
from rock.sandbox.operator.k8s.provider import TemplateSpec
from rock.sdk.common.exceptions import BadRequestRockError

logger = init_logger(__name__)

template_router = APIRouter()


@template_router.post("/templates")
@handle_exceptions(error_message="create template failed")
async def create_template(request: TemplateCreateRequest) -> RockResponse[TemplateCreateResponse]:
    """Create or reuse a template.

    Idempotent: same non-null spec fields produce the same templateID.
    Capacity is excluded from identity; use /templates/{id}/scale to adjust it.
    Returns templateID and status.
    """
    spec = TemplateSpec(
        from_image=request.from_image,
        cpu_count=request.cpu_count,
        memory_mb=request.memory_mb,
        disk_gb=request.disk_gb,
        num_gpus=request.num_gpus,
        accelerator_type=request.accelerator_type,
    )
    result = await sandbox_api.sandbox_manager.create_template(spec)
    return RockResponse(result=TemplateCreateResponse(**result))


@template_router.get("/templates/{template_id}")
@handle_exceptions(error_message="get template status failed")
async def get_template_status(template_id: NonBlankStr) -> RockResponse[TemplateStatusResponse]:
    """Query template status by templateID.

    Returns 404-equivalent (status=Failed) if template_id not found.
    """
    status = await sandbox_api.sandbox_manager.get_template_status(template_id)
    if status is None:
        raise BadRequestRockError(f"Template {template_id} not found")
    return RockResponse(result=TemplateStatusResponse(**status))


@template_router.post("/templates/{template_id}/scale")
@handle_exceptions(error_message="scale template failed")
async def scale_template(
    template_id: NonBlankStr,
    request: TemplateScaleRequest,
) -> RockResponse[TemplateStatusResponse]:
    """Scale a template's Pool capacity.

    PATCH semantics: only provided capacity fields are updated.
    """
    capacity = request.model_dump(exclude_unset=True, by_alias=False)
    result = await sandbox_api.sandbox_manager.scale_template(template_id, capacity)
    return RockResponse(result=TemplateStatusResponse(**result))


@template_router.delete("/templates/{template_id}")
@handle_exceptions(error_message="delete template failed")
async def delete_template(template_id: NonBlankStr) -> RockResponse[str]:
    """Delete a template (Pool CRD).

    Returns success when deleted or not found. K8s/Pool controller is responsible
    for protecting a pool that is still referenced by running sandboxes.
    """
    await sandbox_api.sandbox_manager.delete_template(template_id)
    return RockResponse(result=f"{template_id} deleted")
