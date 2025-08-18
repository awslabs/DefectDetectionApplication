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

import random
import weakref
import threading

from panorama import apidefs
from panorama import panorama_projections

class Upcasting:
    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        cls._lock.acquire()
        if cls._instance is None:
            cls._instance = super(Upcasting, cls).__new__(cls)
            cls._mapping = {}

        cls._lock.release()
        return cls._instance

    def register_object(self, object):
        oid = id(object)
        self._lock.acquire()
        self._mapping[oid] = weakref.ref(object)
        self._lock.release()
        return oid
    
    def unregister_object(self, id):
        self._lock.acquire()
        if id in self._mapping:
            del self._mapping[id]
        self._lock.release()

    def get_object(self, id):
        obj = None

        self._lock.acquire()
        if id in self._mapping:
            obj = self._mapping[id]
        self._lock.release()

        if obj is None:
            return obj

        return obj()

def Upcast(object):
    return Upcasting().get_object(object.ObjectId())

# Python implementation of IUnknownAlias
class UnknownImpl(panorama_projections.IUnknownAlias):
    def __init__(self):
        # Number of references by native code
        panorama_projections.IUnknownAlias.__init__(self)
        self.ref = 0
        self.uuid = "00000000-0000-0000-0000-000000000001"
        self.object_id = Upcasting().register_object(self)

    def __del__(self):
        Upcasting().unregister_object(self.object_id)

    def AddRef(self):
        self.ref = self.ref + 1
        return self.ref

    def Release(self):
        self.ref = self.ref - 1
        if(self.ref == 0):
            panorama_projections.PyObjectRelease(self)

        return self.ref
    
    def RefCount(self):
        return self.ref
    
    def QueryInterface(self, uuid):
        if self.uuid == uuid:
            return apidefs.S_OK
        else:
            return apidefs.E_NOINTERFACE
        
    def ObjectId(self):
        return self.object_id