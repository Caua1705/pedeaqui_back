import logging
from typing import Any

from fastapi import Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from starlette.responses import Response


logger = logging.getLogger("uvicorn.error")

_DELIVERY_SUFFIX = "/delivery/estimate"
_ADDRESS_IMPORT_PATH = "/customers/me/addresses/import"


def _payload_summary(path: str, body: Any) -> str:
    if not isinstance(body, dict):
        return "body_type=invalid"
    if path.endswith(_DELIVERY_SUFFIX):
        branch_id = body.get("branch_id")
        return (
            f"address_id_present={bool(body.get('address_id'))} "
            f"address_present={isinstance(body.get('address'), dict)} "
            f"branch_id_empty={branch_id is None or branch_id == ''}"
        )
    addresses = body.get("addresses")
    count = len(addresses) if isinstance(addresses, list) else "invalid"
    return f"addresses_count={count}"


async def log_contract_validation_error(
    request: Request,
    exc: RequestValidationError,
) -> Response:
    path = request.url.path
    if path.endswith(_DELIVERY_SUFFIX) or path == _ADDRESS_IMPORT_PATH:
        first_error = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first_error.get("loc", ()))
        reason = str(first_error.get("msg", "invalid payload"))
        logger.warning(
            "[Contract validation] route=%s %s field=%s reason=%s",
            path,
            _payload_summary(path, exc.body),
            location or "body",
            reason,
        )
    return await request_validation_exception_handler(request, exc)
