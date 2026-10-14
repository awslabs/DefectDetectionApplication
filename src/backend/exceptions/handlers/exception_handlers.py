#
#  Copyright 2025 Amazon Web Services, Inc.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import sys

from typing import Union

from fastapi import Request
from fastapi.exceptions import RequestValidationError, HTTPException

from fastapi.responses import JSONResponse
from fastapi.responses import Response
from asgi_correlation_id import correlation_id

import logging

from exceptions.api.base_types.validation_exception import ValidationException

logger = logging.getLogger(__name__)

def get_request_id():
    return correlation_id.get()
 

#: Request body keys whose values are never logged: stream camera
#: credentials and the like (rtsp-rtmp-stream-cameras Requirement 6.1).
_SENSITIVE_BODY_KEYS = frozenset({"credentials", "password", "passwd", "urlsecret", "secret", "token"})


def _body_for_log(body):
    """``body`` with the values of sensitive keys masked. A raw (unparsed)
    body naming one of those keys is not logged at all, since its values
    cannot be told apart from the rest."""
    if isinstance(body, dict):
        return {key: ("***" if str(key).lower() in _SENSITIVE_BODY_KEYS and value not in (None, "", {})
                      else _body_for_log(value))
                for key, value in body.items()}
    if isinstance(body, (list, tuple)):
        return [_body_for_log(item) for item in body]
    if isinstance(body, (bytes, str)):
        text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        if any(key in text.lower() for key in _SENSITIVE_BODY_KEYS):
            return "[body withheld: it may contain credentials]"
    return body


async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    This is a wrapper to the default RequestValidationException handler of FastAPI.
    This function will be called when client input is not valid.
    """
    query_params = request.query_params  # pylint: disable=protected-access
    errors = exc.errors()
    safe_errors = [dict(error, input=_body_for_log(error["input"])) if "input" in error else error
                   for error in errors]
    detail = {"errors": safe_errors, "body": _body_for_log(exc.body), "query_params": query_params}
    logger.error(detail)
    # An error whose input carries a credential would echo it in str(exc);
    # only then is the message rebuilt from the masked errors, so every
    # other response keeps its exact text.
    message = str(exc) if safe_errors == errors else "{} validation error(s): {}".format(
        len(safe_errors), safe_errors)
    return JSONResponse({'message': message, 'request_id': get_request_id()}, status_code = 400)


async def http_exception_handler(request: Request, exc: HTTPException) -> Union[JSONResponse, Response]:
    """
    This is a wrapper to the default HTTPException handler of FastAPI.
    This function will be called when a HTTPException is explicitly raised.
    """
    return JSONResponse({'message':str(exc.detail), 'request_id': get_request_id()}, status_code = exc.status_code)

async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    This middleware will log all unhandled exceptions.
    Unhandled exceptions are all exceptions that are not HTTPExceptions, RequestValidationErrors or custom Exceptions.
    """
    exception_type, exception_value, exception_traceback = sys.exc_info()
    exception_name = getattr(exception_type, "__name__", None)

    logger.error("Uncaught Exception", exc_info=(exception_type, exception_value, exception_traceback))

    return JSONResponse({'message': f"Internal Server Error. Error: '{exception_name}: {exception_value}.'", 'request_id': get_request_id()}, status_code=500)

def api_exception_logger(request: Request, exc):
    url = f"{request.url.path}?{request.query_params}" if request.query_params else request.url.path
    exception_type, exception_value, exception_traceback = sys.exc_info()
    exception_name = getattr(exception_type, "__name__", None)
    logger.warning(
        f'"{request.method} {url}" Caught Exception <{exception_name}: {exception_value}. Code: 400',
        exc_info=(exception_type, exception_value, exception_traceback)
    )

async def validation_exception_handler(request: Request, exc: ValidationException) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server is unable to process the request because of a validation error. Error: " + f"'{str(exc.as_validation_exception())}'"
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)

async def pipeline_execution_exception_handler(request: Request, exc) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server is unable to process the request because of a pipeline processing error. Error: " + f"'{str(exc)}' " + "Check the pipeline and retry again."
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)


async def pipeline_syntax_exception_handler(request: Request, exc) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server is unable to process the request because of a pipeline syntax error. Error: " + f"'{str(exc)}' " + "Check the pipeline syntax and retry again."
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)

async def captured_image_exception_handler(request: Request, exc) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server is unable to process image. Error: " + f"'{str(exc)}' " + "Check the error message and retry again."
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)

async def image_not_found_exception_handler(request: Request, exc) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server is unable to find image on location. Error: " + f"'{str(exc)}' " + "Check the error message and retry again."
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)

async def grpc_exception_handler(request: Request, exc) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server received an error from Amazon Lookout for Vision Edge Agent. Error: " + f"'{str(exc)}'" + " Check the error message and retry again."
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)

async def aravis_camera_exception_handler(request: Request, exc) -> JSONResponse:
    api_exception_logger(request, exc)
    err_msg = "The server received an error from camera. Check error message and retry again. Make sure camera is not unplugged and not connected to another device. Error: " + f"'{str(exc)}' "
    return JSONResponse({'message':err_msg, 'request_id': get_request_id()}, status_code = exc.status_code)

