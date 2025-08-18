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

# This script is triggered by the pipeline in order to interact with digital output
# Original script is present here -
# https://code.amazon.com/packages/NeoAgentSmith/blobs/mainline/--/examples/gst_emoutputevent_example/dio.py
# The implementation is same as above with variation in naming
# python3 dio.py -c '[{"pin": "245", "pulseWidth": 500, "signalType": "GPIO.RISING", "rule": "Normal"}]' -a 0 -f 0.9

import argparse
import json
import threading
import time
import logging

from utils.constants import ANOMALY, NORMAL, GPIO_FALLING, GPIO_RISING
logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)

try:
    from periphery import GPIO
except:
    logging.error("ERROR: periphery GPIO library isn't present, exiting")
    exit(1)


def __set_pin(pin: int, pin_value: bool, direction:str):
    try:
        _gpio_out = GPIO(line=pin, direction=direction)
        _gpio_out.write(pin_value)
        _gpio_out.close()
        return True, None
    except Exception as e:
        return False, e


def set_output_pin(pin: int, signal_type: str):
    '''
    Function set a pin value 
    '''
    if signal_type == GPIO_RISING:
        pin_value = True
    elif signal_type == GPIO_FALLING:
        pin_value = False
    else:
        logging.error(f"Unknown signal type {signal_type}")
        exit(1)

    # set the pin
    is_success, error = __set_pin(pin, pin_value, "out")
    if not is_success:
        logging.error(f"Error while setting pin {str(pin)} to {signal_type}. {error}")
        exit(1)


def reset_output_pin(pin: int, signal_type: str):
    if signal_type == GPIO_RISING:
        pin_value = False
    elif signal_type == GPIO_FALLING:
        pin_value = True
    else:
        logging.error(f"Unknown signal type {signal_type}")
        exit(1)

    # reset the pin
    is_success, error = __set_pin(pin, pin_value, "out")
    if not is_success:
        logging.error(f"Error while resetting pin {str(pin)}. {error}")
        exit(1)


def enforce_rule(config, is_anomalous, confidence):
    result = NORMAL
    if is_anomalous:
        result = ANOMALY
    pin = int(config['pin'])

    if config["rule"] == result or config["rule"] == "All":
        set_output_pin(pin, config["signalType"])
        logging.info(f"pin {str(pin)} set to {config['signalType']} for {str(config['pulseWidth'])} ms")

        ## Output latching mode enabled
        if config["pulseWidth"] <= 0:
            return

        # else, wait till "pulseWidth" and then reset the pin
        time.sleep(config["pulseWidth"] / 1000)
        reset_output_pin(pin, config["signalType"])
# TODO: Merge this with main in future.
def direct_execute(configs_str, is_anomalous, confidence):
    try:
        configs = json.loads(configs_str)
    except:
        logging.error("Error when loading config")
        exit(1)
    logging.info(f"Config loaded: {configs}")
    is_anomalous = bool(is_anomalous)
    confidence = float(confidence)
    logging.info(f"is_anomalous: {is_anomalous}")
    logging.info(f"confidence: {confidence}")
    threads = [threading.Thread(target=enforce_rule, args=[config,is_anomalous,confidence]) for config in configs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-c", "--configs", required=True)
    parser.add_argument("-a", "--is_anomalous", type=int, required=True)
    parser.add_argument("-f", "--confidence", type=float, required=True)
    args = parser.parse_args()

    try:
        configs = json.loads(args.configs)
    except:
        logging.error("Error when loading config")
        exit(1)
    logging.info(f"Config loaded: {configs}")

    is_anomalous = bool(args.is_anomalous)
    confidence = float(args.confidence)
    logging.info(f"is_anomalous: {is_anomalous}")
    logging.info(f"confidence: {confidence}")

    threads = []
    for config in configs:
        threads.append(
            threading.Thread(target=enforce_rule, args=[config, is_anomalous, confidence])
        )
    for thread in threads:
        thread.start()

    for thread in threads:
        thread.join()
