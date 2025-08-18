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

import os
import json
import importlib.util
from panorama import trace, messagebroker
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base

class SqlProtocolMessageImpl(messagebroker.PyProtocolMessage):
    """
    Sql protocol message implementation

    Args:
        payload: message payload
        message_options: message options, requires: model_class
    """
    def __init__(self, payload, message_options):
        messagebroker.PyProtocolMessage.__init__(self)
        self._payload = payload
        self._message_options = message_options

    def payload(self):
        return self._payload

    def message_options(self):
        return self._message_options

class SqliteProtocolFactoryImpl(messagebroker.PyProtocolFactory):
    """
    Sql protocol factory implementation

    """
    def __init__(self):
        messagebroker.PyProtocolFactory.__init__(self)

    def create_protocol(self, creation_options, credential_provider):
        """
        Creates protocol client

        creation_options: static options, requires: imports_file, session_class
        credential_provider: unused
        """
        if 'imports_file' in creation_options and 'session_class' in creation_options:
            return SqliteProtocolClient(creation_options['imports_file'],
                                        creation_options['session_class'])
        raise RuntimeError("Invalid creation options")

    def validate_message_options(self, message_options):
        return "model_class" in message_options

    def create_message(self, payload, message_options):
        return SqlProtocolMessageImpl(payload, message_options)

    def protocol_name(self):
        return "sqlite_protocol_client"

class SqliteProtocolClient(messagebroker.PyProtocolClient):
    """
    Sql protocol client implementation

    imports_file: app script that allows loading the schema and session classes
    session_class: app session class
    """
    def __init__(self, imports_file, session_class):
        messagebroker.PyProtocolClient.__init__(self)
        self.imports_file=imports_file
        self.session_class=session_class
        self.imports=None
        self.session=None

    def getImports(self):
        if self.imports:
            return self.imports

        if not self.imports_file.endswith('.py'):
            raise RuntimeError('invalid imports file input')

        module_name=self.imports_file.split('/')[-1][:-3] # /<>/module.py -> module
        spec=importlib.util.spec_from_file_location(module_name, self.imports_file)
        self.imports=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.imports)

        return self.imports

    def publish(self, message):
        """
        Writes the payload to the db synchronously

        """
        trace.info(f"sqlite protocol client publish handler")

        payload=message.payload().SerializeAsString()
        payload_json=json.loads(payload)

        model_class_name=message.message_options()['model_class']
        model_class=getattr(self.getImports(), model_class_name)
        result=model_class(**payload_json)

        # write to db
        session_class=getattr(self.getImports(), self.session_class)
        session=session_class()
        session.add(result)
        session.commit()

        trace.info(f"sqlite add entry to table '{model_class_name}' success")

    def publish_async(self, message, eventHandler):
        trace.warning('sql protocol publish_async not implemented')

    def friendly_name(self):
        return "sqlite_protocol_client"
