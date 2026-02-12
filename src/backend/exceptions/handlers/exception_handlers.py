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
 

async def request_validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """
    This is a wrapper to the default RequestValidationException handler of FastAPI.
    This function will be called when client input is not valid.
    """
    query_params = request.query_params  # pylint: disable=protected-access
    detail = {"errors": exc.errors(), "body": exc.body, "query_params": query_params}
    logger.error(detail)
    return JSONResponse({'message':str(exc), 'request_id': get_request_id()}, status_code = 400)


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
    return JSONResponse({'message': "Internal Server Error. Please contact admin.", 'request_id': get_request_id()}, status_code=500)
    #return JSONResponse({'message': f"Internal Server Error. Error: '{exception_name}: {exception_value}.'", 'request_id': get_request_id()}, status_code=500)

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

