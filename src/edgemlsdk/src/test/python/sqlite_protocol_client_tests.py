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

import os
import json
from panorama import messagebroker, trace, credentials
from panorama.sqlite_protocol_client import SqliteProtocolFactoryImpl
from test_db_model import Session, User, DB_FILE
import test_utils

trace.set_trace_level(trace.TraceLevel.Info)
trace.add_console_trace_listener()

class SqliteProtocolClientTests:
    def __init__(self):
        self.config= """
              {
                "targets": [
                    {
                        "protocol": "sqlite_protocol_client",
                        "name": "test_target",
                        "sqlite_protocol_client_options": {
                            "imports_file": "test_db_model.py",
                            "session_class": "Session"
                        }
                    }
                ],
                "pipes": [
                    {
                        "message_id": "message_id",
                        "destinations": [
                            {
                                "target_name": "test_target",
                                "sqlite_protocol_client_message_options": {
                                    "model_class": "User"
                                }
                            }
                        ]
                    }
                ]
            }
        """
        creds = credentials.create_default_aws_credential_provider()
        messagebroker.set_default_config(self.config)
        self.broker = messagebroker.create(creds)

        factory = SqliteProtocolFactoryImpl()
        self.broker.add_protocol_factory(factory)
        self.broker.initialize()

    def test_publish_happy_path(self):
        data = {'username': 'jack'}
        payload=messagebroker.create_payload_from_string(json.dumps(data))
        res=self.broker.publish("message_id", payload)

        # verify data is written to db
        test_utils.Expect_Equal(Session().query(User).filter_by(username='jack').first().username, "jack")
        trace.info('verified readback from db success')

def run_tests():
    os.remove(DB_FILE)
    tests = SqliteProtocolClientTests()
    tests.test_publish_happy_path()

run_tests()
