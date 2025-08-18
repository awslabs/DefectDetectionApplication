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

# This script contains utility functions for digital outputs
# python3 dio_utils.py -a RESET -c '[{"pin": "245", "pulseWidth": 500, "signalType": "GPIO.RISING", "rule": "Normal"}]'
from utils.constants import GPIO_RISING, GPIO_FALLING

import logging
logger = logging.getLogger(__name__)

try:
    from periphery import GPIO
except Exception as e:
    print("periphery library could not be imported")


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
        logger.error(f"Unknown signal type {signal_type}")

    # set the pin
    is_success, error = __set_pin(pin, pin_value, "out")
    if not is_success:
        logger.error(f"Error while setting pin {str(pin)} to {signal_type}. {error}")


def reset_output_pin(pin: int, signal_type: str):
    if signal_type == GPIO_RISING:
        pin_value = False
    elif signal_type == GPIO_FALLING:
        pin_value = True
    else:
        logger.error(f"Unknown signal type {signal_type}")

    # reset the pin
    is_success, error = __set_pin(pin, pin_value, "out")
    if not is_success:
        logger.error(f"Error while resetting pin {str(pin)}. {error}")
