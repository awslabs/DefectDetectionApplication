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

import time
import socket
import logging
import sys

logging.basicConfig(
    format="%(levelname)s:%(message)s",
    level=logging.DEBUG,
    handlers=[logging.StreamHandler(sys.stdout)],
)


# Function to check if localhost:5000 is reachable
def is_port_open(host, port, requestBy, timeout=10):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (socket.timeout, socket.error) as e:
        logging.debug(f"{requestBy}: Socket exception is {e}")
        return False


# Retry logic to wait for the server to start
def wait_for_server(host, port, requestBy, retries=5, delay=3):
    """
    Waits for the server to be reachable, retrying if necessary.

    :param retries: Number of times to retry before giving up
    :param delay: Seconds to wait between retries
    :return: True if the server is reachable, False otherwise
    """
    for attempt in range(retries):
        if is_port_open(host, port, requestBy):
            logging.info(
                f"{requestBy}: Server is reachable on {host}:{port} with retry:{attempt}. Proceeding with request..."
            )
            return True
        else:
            if attempt < retries - 1:
                sleep_time=delay * (2 ** attempt)
                logging.info(
                    f"{requestBy}: Attempt {attempt + 1}/{retries}: Server not reachable. Retrying in {sleep_time} seconds..."
                )
                time.sleep(sleep_time)

    logging.info(f"{requestBy}: Server is still not reachable after {retries} retries.")
    return False
