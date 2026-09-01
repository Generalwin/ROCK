from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute

from rock.admin.proto.e2b_connect import (
    CONNECT_CONTENT_TYPE,
    CONNECT_REQUEST_LIMIT,
    decode_connect_request,
    encode_connect_end,
    parse_keepalive_interval,
    sandbox_id_from_headers,
    start_command,
)
from rock.admin.proto.response import E2BConnectErrorPayload, E2BListedSandbox
from rock.admin.service.e2b_proxy_service import E2BProxyService
from rock.logger import init_logger
from rock.sdk.common.exceptions import BadRequestRockError, E2BConnectError, SandboxNotFoundRockError

logger = init_logger(__name__)


class E2BProxyAPIRoute(APIRoute):
    def get_route_handler(self):
        route_handler = super().get_route_handler()

        async def handler(request: Request):
            try:
                return await route_handler(request)
            except RequestValidationError as error:
                message = "; ".join(
                    f"{'.'.join(str(part) for part in item['loc'])}: {item['msg']}" for item in error.errors()
                )
                return _error_response(400, message)
            except BadRequestRockError as error:
                logger.warning("E2B proxy request rejected: %s", error)
                return _error_response(400, str(error))
            except Exception:
                logger.exception("E2B proxy request failed")
                return _error_response(500, "Internal server error")

        return handler


e2b_proxy_router = APIRouter(route_class=E2BProxyAPIRoute)
e2b_proxy_service: E2BProxyService


def set_e2b_proxy_service(service: E2BProxyService) -> None:
    global e2b_proxy_service
    e2b_proxy_service = service


def _error_response(status_code: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"code": status_code, "message": message})


@e2b_proxy_router.get(
    "/v2/sandboxes",
    response_model=list[E2BListedSandbox],
    response_model_by_alias=True,
    response_model_exclude_none=True,
)
async def list_sandboxes(
    metadata: Annotated[str, Query(min_length=1)],
) -> list[E2BListedSandbox]:
    return await e2b_proxy_service.list_sandboxes(metadata)


async def _read_connect_body(request: Request) -> bytes:
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > CONNECT_REQUEST_LIMIT + 5:
            raise E2BConnectError("resource_exhausted", "Connect request is too large")
    return bytes(body)


@e2b_proxy_router.post("/process.Process/Start")
async def start_process(request: Request) -> Response:
    try:
        sandbox_id = sandbox_id_from_headers(request.headers)
        payload = decode_connect_request(await _read_connect_body(request))
        command = start_command(sandbox_id, payload, request.headers)
        keepalive_interval = parse_keepalive_interval(request.headers)
        await e2b_proxy_service.require_running_sandbox(sandbox_id)
    except SandboxNotFoundRockError as error:
        return Response(
            content=encode_connect_end(E2BConnectErrorPayload(code="not_found", message=str(error))),
            media_type=CONNECT_CONTENT_TYPE,
        )
    except BadRequestRockError as error:
        return Response(
            content=encode_connect_end(E2BConnectErrorPayload(code="invalid_argument", message=str(error))),
            media_type=CONNECT_CONTENT_TYPE,
        )
    except E2BConnectError as error:
        return Response(
            content=encode_connect_end(E2BConnectErrorPayload(code=error.code, message=error.message)),
            media_type=CONNECT_CONTENT_TYPE,
        )
    except Exception:
        logger.exception("E2B process request preparation failed")
        return Response(
            content=encode_connect_end(
                E2BConnectErrorPayload(code="internal", message="internal server error"),
            ),
            media_type=CONNECT_CONTENT_TYPE,
        )
    return StreamingResponse(
        e2b_proxy_service.start_process(command, keepalive_interval=keepalive_interval),
        media_type=CONNECT_CONTENT_TYPE,
    )


@e2b_proxy_router.get("/health")
async def health(request: Request) -> Response:
    sandbox_id = sandbox_id_from_headers(request.headers)
    status_code = 204 if await e2b_proxy_service.is_running(sandbox_id) else 502
    return Response(status_code=status_code)
