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

from panorama import messagebroker
from panorama import credentials
from panorama import trace

import threading

def OnMessageReceived(payload):
    trace.info(f"Received message {payload.serialize_as_string()}")

def OnLocalMessage(payload):
    trace.info(f"Received locally: {payload.serialize_as_string()}")

def OnPublished(protocol, message_id, Payload, successful):
    if successful:
        trace.info(f"Succesfully published {message_id} on {protocol}")
    else:
        trace.error(f"Succesfully published {message_id} on {protocol}")

def main():
    trace.add_console_trace_listener()

    """
        The following configuration has the following behavior:
        - Two targets created: MQTT and S3
        - Subscribing to subscription-id `test-subscription` will receive messages published onto mqtt topic 'broker-test-subscription'
        - Messages published with message-id 'test_message' will be published onto mqtt topic 'broker-test-publish'
        - Messages published with message-id 'big_data' will be saved to S3 at s3://panorama-sdk-v2-artifacts/test/broker_sample
            - Optional "overwrite" flag is not specified, so default behavior will be to overwrite any existing contents of the bucket/key
    """
    config = """{
        "targets": [                                                              
            {                                                                       
                "protocol": "mqtt",                                             
                "name": "test-mqtt",                                            
                "mqtt_options": {                                                 
                    "endpoint": "a5h6960s3xow6-ats.iot.us-west-2.amazonaws.com",
                    "region": "us-west-2"                                       
                },                                                                  
                "mqtt_subscriptions": [                                           
                    {                                                               
                        "subscription_id": "test-subscription",                 
                        "topic": "broker-test-subscription"                     
                    }                                                               
                ]                                                                   
            },                                                                      
            {                                                                       
                "protocol": "s3",                                               
                "name": "test-s3",                                              
                "s3_options": {                                                   
                    "region": "us-west-2"                                       
                }                                                                   
            }                                                                       
        ],                                                                          
        "pipes": [                                                                
            {                                                                       
                "message_id": "test_message",                                   
                "destinations": [                                                 
                    {                                                               
                        "target_name": "test-mqtt",                             
                        "mqtt_message_options": {                                 
                            "topic": "broker-test-publish"                      
                        }                                                           
                    }                                                               
                ]                                                                   
            },                                                                      
            {                                                                       
                "message_id": "big_data",                                       
                "destinations": [                                                 
                    {                                                               
                        "target_name": "test-s3",                               
                        "s3_message_options": {                                   
                            "bucket": "panorama-sdk-v2-artifacts",              
                            "key": "test/broker_sample"                         
                        }                                                           
                    }                                                               
                ]                                                                   
            }                                                                       
        ]                                                                           
    }"""

    # Get the credential provider for AWS credentials set in your environment variables
    creds = credentials.create_default_aws_credential_provider()
    
    # Set the default configuration to the string defined above
    messagebroker.set_default_config(config)

    # Create the message broker without expclitly passing the configuration data
    broker = messagebroker.create(creds)

    # Broker initialize must be called to instantiate target protocols and hook up pipes
    # Safe to call multiple times.
    broker.initialize()

    # Subscribe to test-subscription
    # Will be invoked when mqtt topic 'broker-test-subscription' is published too
    token1 = broker.subscribe("test-subscription", OnMessageReceived)
    
    # In addition to publishing/subscribing to predefined targets you can locally subscribe and publish
    # Here we are subscribing to the "test_message" subscription-id.  Will be invoked (in addition to routing to mqtt)
    # when a publish happens with message_id = `test_message`
    token2 = broker.subscribe("test_message", OnLocalMessage)

    # Create some payload for publishing
    payload = messagebroker.create_payload_from_string("hello world")

    # Publish messages
    broker.publish_async("test_message", payload, OnPublished)
    broker.publish_async("big_data", payload, OnPublished)

    trace.info("Press any key to exit")
    input()

    broker.unsubscribe(token1)
    broker.unsubscribe(token2)

if __name__ == "__main__":
    main()