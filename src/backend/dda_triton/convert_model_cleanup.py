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

import argparse
import logging
import os
import shutil
from dda_triton.constants import TRITON_MODEL_DIR
import sys
import requests
from dda_triton.model_autostart_utils import wait_for_server

parser = argparse.ArgumentParser(description=" Script converts model to Triton format")
parser.add_argument("--model_name", help="Model name")
logging.basicConfig(
    format="%(levelname)s:%(message)s",
    level=logging.DEBUG,
    handlers=[logging.StreamHandler(sys.stdout)],
)


def stop_model(model_name):
    if wait_for_server("localhost", 5000, "StopModel"):
        url = f"http://localhost:5000/feature-configurations/models/{model_name}/stop"
        headers = {"accept": "application/json"}
        response = requests.get(url, headers=headers)

        if response.status_code == 200:
            logging.info("Model stopped successfully!")
        else:
            logging.error(f"StopModel: Request failed with status code: {response.status_code}")
            logging.error(response.text)
    else:
        logging.info("StopModel: localserver:5000 is not reachable")


if __name__ == "__main__":
    try:
        args = parser.parse_args()
        if args.model_name:
            model_name = args.model_name
            stop_model(model_name)
            model_paths_to_clear = [f"base_{model_name}", f"marshal_{model_name}", model_name]
            for path in model_paths_to_clear:
                dir_to_clean = os.path.join(TRITON_MODEL_DIR, path)
                if os.path.exists(dir_to_clean) and os.path.isdir(dir_to_clean):
                    shutil.rmtree(dir_to_clean)
                    logging.info(f"Cleaned directory: {dir_to_clean}")
            logging.info(f"Directory cleanup finished")
        else:
            raise Exception("Args not provided to perform the cleanup.")
    except Exception as e:
        logging.error(f"Exception occurred while cleaning model dir: {str(e)}")
        exit(1)
