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
import threading

namespace_lock = threading.Lock()
namespace = {}
counters = {}


class NamespaceLock:
    def __init__(self, group):
        self.group = group

    def __enter__(self):
        self.__class__.acquire_lock(self.group)

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.__class__.release_lock(self.group)

    @staticmethod
    def acquire_lock(value, blocking=True, timeout=-1.0):
        with namespace_lock:
            if value in namespace:
                counters[value] += 1
            else:
                namespace[value] = threading.Lock()
                counters[value] = 1

        return namespace[value].acquire(blocking=blocking, timeout=timeout)

    @staticmethod
    def release_lock(value):
        with namespace_lock:
            if counters[value] == 1:
                del counters[value]
                lock = namespace.pop(value)
            else:
                counters[value] -= 1
                lock = namespace[value]

        lock.release()
