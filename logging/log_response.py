# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
"""SafeBoard replacement for vLLM's response-body middleware.

Used only when VLLM_DEBUG_LOG_API_SERVER_RESPONSE is set. Logs the path,
status, and size. Does not write the response body.
"""

from fastapi import Request
from starlette.concurrency import iterate_in_threadpool

from vllm.logger import init_logger

logger = init_logger(__name__)


async def log_response(request: Request, call_next):
    response = await call_next(request)
    response_body = [section async for section in response.body_iterator]
    response.body_iterator = iterate_in_threadpool(iter(response_body))
    nbytes = sum(len(section) for section in response_body if section)
    logger.info(
        "model response path=%s status=%s bytes=%s (body omitted)",
        request.url.path,
        response.status_code,
        nbytes,
    )
    return response
