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
from time import perf_counter
from asgi_correlation_id import correlation_id

import logging
logger = logging.getLogger(__name__)

class Timer(object):
    def __init__(self, metric_name):
        self.metric_name = metric_name

        request_id = correlation_id.get()
        if request_id:
            self.request_id = request_id
        else:
            self.request_id = '-'
        self.elapsed_time = None

    def __enter__(self):
        self.elapsed_time = perf_counter()
        return self

    def __exit__(self, type, value, traceback):
        self.elapsed_time = round((perf_counter() - self.elapsed_time) * 1000, 2)
        logger.info(f"RequestID={self.request_id}, MetricName={self.metric_name}, MetricValue={self.elapsed_time}ms")
