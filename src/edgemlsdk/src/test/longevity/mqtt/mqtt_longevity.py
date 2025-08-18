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

import time
import sys
from panorama import trace
from panorama import panorama_version
from panorama import application
from panorama import messagebroker
from panorama import buffer
from panorama import credentials
import os
from botocore.credentials import InstanceMetadataProvider, InstanceMetadataFetcher
import threading


class MqttMessageBrokerLongevityTests:
    def __init__(self, app) -> None:
        self.app = app
        self.session = self.app.create_boto3_session()
        self.message_received = threading.Event()
        try:
            self.mqtt_endpoint = self.app.get_property("mqtt_endpoint")
            self.longevity_hours = app.get_property("longevity_hours")
            self.payload_size = app.get_property("payload_size")
        
        except Exception as e:
            trace.error("mqtt_endpoint, longevity_hours, or payload_size was not defined.")
            exit(1)

        if self.session.region_name is None:
            self.aws_region = app.get_property("region").get_value()
        else: self.aws_region = self.session.region_name

        self.create_mqtt_message_broker()

    def create_mqtt_message_broker(self):
        try:
            self.mqtt_msg_broker = messagebroker.create_mqtt_message_broker(
                self.mqtt_endpoint.get_value(),
                self.aws_region,
                self.app
            )
        except Exception as e:
            trace.error("Failed to create mqtt message broker with error - ", e)
            exit(1)

    def publish(self, payload):
        try:
            status = self.mqtt_msg_broker.publish(payload)
            return status
        except Exception as e:
            trace.error("Failed to publish payload")
            return -1

    def subscribe(self, topic, callback):
        try:
            status = self.mqtt_msg_broker.subscribe(topic, callback)
            return status
        except Exception as e:
            trace.error("Failed to subscribe to topic")
            return -1

    def subscribe_callback(self, topic, payload):
        trace.info(f"Received message on topic {topic} with payload size : {self.payload_size.get_value()}KB")
        self.message_received.set()
        if payload.as_string() != "A"*self.payload_size.get_value()*1024:
            trace.error("Received Payload doesn't match Published Payload")

    def unsubscribe(self, topic):
        try:
            status = self.mqtt_msg_broker.unsubscribe(topic)
            return status
        except Exception as e:
            trace.error("Failed to Unsubscribe topic")
            return -1
    
    
def main(args):
    trace.add_console_trace_listener()
    trace.info(f"Initializing EdgeML SDK {panorama_version.__version__}")
    app = application.create(args)
    topic = "mqtt_test_topic"
    longevity = MqttMessageBrokerLongevityTests(app)
    payload_str = "A"*(1024*longevity.payload_size.get_value())
    payload = buffer.create_from_string(payload_str)
    payload = messagebroker.create_mqtt_message(topic, payload)
    longevity.subscribe(topic, longevity.subscribe_callback)
    start_time = time.perf_counter()
    longevity_time_in_seconds = longevity.longevity_hours.get_value() * 60 * 60
    credentials_reset_timer = time.perf_counter()
    pub_req_count = 0
    

    while (time.perf_counter() - start_time) < longevity_time_in_seconds:
        longevity.publish(payload)
        longevity.message_received.wait(3)
        longevity.message_received.clear()
        pub_req_count += 1
        if pub_req_count % 10 == 0:
            longevity.unsubscribe(topic)
            longevity.subscribe(topic, longevity.subscribe_callback)
            # give time for subscribe req to complete on mqtt broker side
            time.sleep(1)
            
       
    longevity.unsubscribe(topic)

if __name__ == "__main__":
    args = None
    if len(sys.argv) - 1 > 0:
        args = [bytes(arg, encoding="utf-8") for arg in sys.argv[1:]]

    main(args)
 
 # usage python3 mqtt_longevity.py --trace_level INFO --mqtt_endpoint <endpoint> --longevity_hours 1 --region <region> --payload_size 50