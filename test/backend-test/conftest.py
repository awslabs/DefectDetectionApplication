# Copyright 2025 Amazon Web Services, Inc.
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
import pytest
import unittest.mock as mock
import mock_logger
from unittest.mock import patch
from mock_gi import bogus_gi_module

import pydantic
from typing import List
import shutil
from constants import EXAMPLE_IMAGE_LIST
import os
import sarge
_import = __import__
os.environ["TRITON_INSTALL_DIR"]= "/opt/tritonserver"
# Needed since triton will look for the correct interpreter to run the python backend with.

os.environ["PYTHONHOME"] = "/usr/bin/python3.9"
def import_mocker(name, *args,**kwargs):
    if name == 'dda_logging.logger':
        return mock_logger
    elif name == 'asgi_correlation_id':
        return mock.Mock()
    elif name == 'asgi_correlation_id.context':
        return mock.Mock()
    else:
        return _import(name,*args,**kwargs)


@pytest.fixture(autouse=True)
def mock_log_fixture():
    with mock.patch('builtins.__import__', side_effect=import_mocker):
        mock_add_middleware = patch('fastapi.FastAPI.add_middleware').start()
        mock_add_middleware.return_value = None
        mock_request_id = patch('exceptions.handlers.exception_handlers.get_request_id').start()
        mock_request_id.return_value = 'test'
        mock_path = patch('fastapi.Path').start()
        mock_path.return_value = True
        patch('pydantic.RootModel',pydantic.BaseModel, create=True).start()
        yield

@pytest.fixture(scope="function")
def caplog(request, caplog):
    request.cls.caplog = caplog

@pytest.fixture(scope="session",autouse=True)
def setup_teardown_actions():
    test_dir = "test/backend-test/utils"
    infer_out_dir = "inference_out_for_test/"
    pytest.infer_out_path_orig = os.path.join(os.getcwd(), test_dir, infer_out_dir)
    pytest.infer_out_path = os.path.join(os.getcwd(), test_dir, 'tmp/')
    if os.path.exists(pytest.infer_out_path):
        shutil.rmtree(pytest.infer_out_path)
    shutil.copytree(pytest.infer_out_path_orig, pytest.infer_out_path)

    sorted_files = [EXAMPLE_IMAGE_LIST[key] for key in sorted(EXAMPLE_IMAGE_LIST, reverse=True)]

    for file_name in sorted_files:
        source_file_path = os.path.join(pytest.infer_out_path_orig, file_name)
        destination_file_path = os.path.join(pytest.infer_out_path, file_name)

        if os.path.exists(destination_file_path):
            os.remove(destination_file_path)
            
        shutil.copy2(source_file_path, pytest.infer_out_path)

    yield
    
    if os.path.exists(pytest.infer_out_path):
        shutil.rmtree(pytest.infer_out_path)
