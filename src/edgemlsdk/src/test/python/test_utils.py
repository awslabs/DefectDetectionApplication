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

import inspect
import os
from panorama import apidefs
from panorama import panorama_projections

def Expect_Success(hr):
    if apidefs.FAILED(hr):
        frame = inspect.currentframe().f_back
        raise Exception(f"-------> [{frame.f_lineno}] Command was not successful")
    
def Expect_Fail(hr):
    if apidefs.SUCCEEDED(hr):
        frame = inspect.currentframe().f_back
        raise Exception(f"-------> [{frame.f_lineno}] Command did not fail as expected")

def Expect_Equal(expected, actual):
    if expected != actual:
        frame = inspect.currentframe().f_back
        raise Exception(f"-------> [{frame.f_lineno}] Expected {expected} received {actual}")

def Expect_NotEqual(expected, actual):
    if expected == actual:
        frame = inspect.currentframe().f_back
        raise Exception(f"-------> [{frame.f_lineno}] Expected {expected} received {actual}")

def Expect_True(expected):
    if expected != True:
        frame = inspect.currentframe().f_back
        raise Exception(f'-------> [{frame.f_lineno}] Expect True failed')
    
def Expect_False(expected):
    if expected != False:
        frame = inspect.currentframe().f_back
        raise Exception(f'-------> [{frame.f_lineno}] Expect False failed')

def Expect_Exception(cb):
    try:
        cb()
    except Exception:
        pass
    else:
        frame = inspect.currentframe().f_back
        raise Exception(f"-------> [{frame.f_lineno}] Expected exception, but not thrown")
    
def memcheck():
    panorama_projections.memcheck()