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

import typing
import uuid
import random
import json
import inspect

from abc import ABC, abstractmethod

from panorama import panorama_projections
from panorama import trace
from panorama import unknown
from panorama import apidefs
from panorama import credentials
from panorama import buffer

class Payload(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def serialize(self):
        res = self.native_pointer().Serialize()
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: buffer.Buffer(x))

    def serialize_as_string(self):
        return self.native_pointer().SerializeAsString()

    def id(self):
        return self.native_pointer().Id()

    def timestamp(self):
        return self.native_pointer().Timestamp()
    
    def set_timestamp(self, timestamp : int):
        apidefs.CHECKHR(self.native_pointer().SetTimestamp(timestamp))
    
    def correlation_id(self):
        return self.native_pointer().CorrelationId()
    
    def set_correlation_id(self, correlation_id : str):
        apidefs.CHECKHR(self.native_pointer().SetCorrelationId(correlation_id))

    def uuid():
        # Must equal IPayload UUID
        return "A0BE4CF1-0241-4157-B7F1-4E5D35D92990"

    def query_interface(self, target):
        # Use native pointer of queried type for inheritance
        if target == BatchPayload:
            ret = panorama_projections.PythonQueryInterfaceBatchPayload(self._native)
            if apidefs.FAILED(ret[0]):
                return None
            else:
                return apidefs.assign(ret[1], lambda x: target(x))
        else:
            # Default case, native pointer will be of the original type
            ret = panorama_projections.PythonQueryInterface(self._native, target.uuid())
            if apidefs.FAILED(ret):
                return None
            else:
                return apidefs.assign(self._native, lambda x: target(x))

class BatchPayload(Payload):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal IBatchPayload UUID
        return "789123A1-40BC-4773-A246-F183D307E219"

    def count(self):
        return self.native_pointer().Count()

    def payload(self, i : typing.Union[str, int]):
        res = self.native_pointer().Payload(i)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: Payload(x))

    def add_payload(self, payload: Payload):
        apidefs.CHECKHR(self.native_pointer().AddPayload(payload.native_pointer()))

class ProtocolMessage(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def payload(self):
        res = self.native_pointer().Payload()

        # Depending on scenario both of these are valid options
        if isinstance(res[1], Payload):
            return res[1]
        else:
            return apidefs.attach(res[1], lambda x: Payload(x))
        
    def uuid():
        # Must equal IProtocolMessage UUID
        return "820312E3-4F2E-4585-8E4C-134180847184"

class ProtocolSubscription(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def uuid():
        # Must equal IProtocolSubscription UUID
        return "BC511613-FF74-47FC-AAB5-8F7AF25B467F"

class ProtocolClientEventHandler(unknown.UnknownImpl, panorama_projections.IProtocolClientEventHandler):
    def __init__(self, received_cb : typing.Callable[[Payload], None], publish_cb : typing.Callable[[str, ProtocolMessage, bool], None]):
        panorama_projections.IProtocolClientEventHandler.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "84FC98E3-DDE3-4F09-AF2D-5F1DC7AFBEF7"
        self.message_received_cb = received_cb
        self.published_cb = publish_cb

    def OnMessageReceived(self, payload):
        if(self.message_received_cb != None):
            py_payload = apidefs.assign(payload, lambda x: Payload(x))
            try:
                self.message_received_cb(py_payload)
            except Exception as e:
                trace.error(f"Exception raised in callback protocol client message received: {e}")

    def OnMessagePublished(self, protocol, message, successful):
        if(self.published_cb != None):
            py_message = apidefs.assign(message, lambda x: ProtocolMessage(x))
            try:
                self.published_cb(protocol, py_message, successful)
            except Exception as e:
                trace.error(f"Exception raised in callback protocol client message published: {e}")

class ProtocolClient(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def subscribe(self, subscription : ProtocolSubscription, cb : typing.Callable[[Payload], None]):
        handler = ProtocolClientEventHandler(cb, None)
        panorama_projections.PyObjectAddRef(handler)
        token = self.native_pointer().Subscribe(subscription.native_pointer(), handler)
        apidefs.CHECKHR(token)
        return token
    
    def unsubscribe(self, cancellation_token : int):
        self.native_pointer().Unsubscribe(cancellation_token)

    def publish(self, message : ProtocolMessage):
        apidefs.CHECKHR(self.native_pointer().Publish(message.native_pointer()))

    def publish_async(self, message : ProtocolMessage, cb : typing.Callable[[str, ProtocolMessage, bool], None]):
        handler = ProtocolClientEventHandler(None, cb)
        panorama_projections.PyObjectAddRef(handler)
        apidefs.CHECKHR(self.native_pointer().PublishAsync(message.native_pointer(), handler))

    def friendly_name(self):
        return self.native_pointer().FriendlyName()
    
    def uuid():
        # Must equal IProtocolClient UUID
        return "B3F32887-02D0-4A8F-9E63-9AC7FFC2FB37"
    
class MqttProtocolClient():
    """
    Class that wraps the Mqtt Protocol Client

    Args:
        endpoint: Endpoint of your mqtt server
        region: The AWS region of the bucket(s) you want to write too
        credential_provider: Provider of AWS credentials
    """
    def __init__(self, endpoint : str, region : str, credential_provider : credentials.CredentialProvider, client_id : str = None):
        res = panorama_projections.CreateMQTTProtocolClient(endpoint, region, credential_provider.native_pointer(), client_id)
        apidefs.CHECKHR(res[0])
        self.client = apidefs.attach(res[1], lambda x: ProtocolClient(x))

    def subscribe(self, topic, cb : typing.Callable[[Payload], None]):
        """
        Subscribes to a topic

        Args:
            topic: Topic to subscribe too
            cb: Callback to invoke when a message is received

        Returns:
            Integer.  Used for unsubscribing
        """
        res = panorama_projections.CreateMqttSubscription(topic)
        apidefs.CHECKHR(res[0])
        subscription = apidefs.attach(res[1], lambda x: ProtocolSubscription(x))
        return self.client.subscribe(subscription, cb)
    
    def unsubscribe(self, cancellation_token : int):
        """
        Unsubscribes for a topic

        Args:
            cancellation_token: Integer returned from the matching subscribe call
        """
        self.client.unsubscribe(cancellation_token)

    def publish(self, topic : str, payload : Payload):
        """
        Publishes a payload onto a topic

        Args:
            topic: Topic to publish too
            paylaod: The contents to publish
        """
        message = self.__create_mqtt_message(topic, payload)
        self.client.publish(message)

    def publish_async(self, topic : str, payload : Payload, cb : typing.Callable[[str, ProtocolMessage, bool], None]):
        """
        Asynchronously publishes a payload onto a topic

        Args:
            topic: Topic to publish too
            paylaod: The contents to publish
        """
        message = self.__create_mqtt_message(topic, payload)
        self.client.publish_async(message, cb)

    def __create_mqtt_message(self, topic : str, payload: Payload):
        res = panorama_projections.CreateMqttMessage(payload.native_pointer(), topic)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: ProtocolMessage(x))
    
class S3ProtocolClient():
    """
    Class that wraps the S3 Protocol Client

    Args:
        region: The AWS region of the bucket(s) you want to write too
        credential_provider: Provider of AWS credentials
    """
    def __init__(self, region : str, credential_provider : credentials.CredentialProvider):
        res = panorama_projections.CreateS3ProtocolClient(region, credential_provider.native_pointer())
        apidefs.CHECKHR(res[0])
        self.client = apidefs.attach(res[1], lambda x: ProtocolClient(x))

    def publish(self, bucket : str, key : str, payload : Payload, overwrite : bool = True, batch_payload_expansion : bool = True):
        """
        Writes payload to s3://<bucket>/<key>

        Args:
            bucket: Name of the bucket
            key: Name of the key
            payload: The contents to write
            overwrite: Whether or not to overwrite the bucket/key combination, if it exists. Optional, defaults to True
            batch_payload_expansion: Whether or not to upload and expand macros for payloads contained within a batch payload, as opposed to the batch payload itself. Optional, defaults to True
        """
        message = self.__create_s3_message(bucket, key, payload, overwrite, batch_payload_expansion)
        self.client.publish(message)

    def publish_async(self, bucket : str, key : str, payload : Payload, cb : typing.Callable[[str, ProtocolMessage, bool], None], overwrite : bool = True, batch_payload_expansion : bool = True):
        """
        Asynchronously writes payload to s3://<bucket>/<key>

        Args:
            bucket: Name of the bucket
            key: Name of the key
            payload: The contents to write
            cb: Method to call when publish operation has completed
            overwrite: Whether or not to overwrite the bucket/key combination, if it exists. Optional, defaults to True
            batch_payload_expansion: Whether or not to upload and expand macros for payloads contained within a batch payload, as opposed to the batch payload itself. Optional, defaults to True
        """
        message = self.__create_s3_message(bucket, key, payload, overwrite, batch_payload_expansion)
        self.client.publish_async(message, cb)

    def __create_s3_message(self, bucket : str, key : str, payload: Payload, overwrite: bool, batch_payload_macro_expanson: bool):
        res = panorama_projections.CreateS3Message(payload.native_pointer(), bucket, key, overwrite, batch_payload_macro_expanson)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: ProtocolMessage(x))
    
class FileProtocolClient():
    """
    Class that wraps the File Protocol Client

    """
    def __init__(self):
        res = panorama_projections.CreateFileProtocolClient()
        apidefs.CHECKHR(res[0])
        self.client = apidefs.attach(res[1], lambda x: ProtocolClient(x))

    def publish(self, payload : Payload, directory : str, filename : str):
        """
        Writes payload to disk

        Args:
            payload: The contents to write
            directory: The name of the directory
            filename: The name of the filed
        """
        message = self.__create_message(directory, filename, payload)
        self.client.publish(message)

    def publish_async(self, payload : Payload, directory : str, filename : str, cb : typing.Callable[[str, ProtocolMessage, bool], None] = None):
        """
        Asynchronously writes payload to disk

        Args:
            payload: The contents to write
            directory: The name of the directory
            filename: The name of the filed
            cb: [Optional] Method to invoke when publishing has completed
        """
        message = self.__create_message(directory, filename, payload)
        self.client.publish_async(message, cb)

    def __create_message(self, directory : str, filename : str, payload : Payload):
        res = panorama_projections.CreateFileProtocolMessage(payload.native_pointer(), directory, filename)
        apidefs.CHECKHR(res[0])
        return apidefs.attach(res[1], lambda x: ProtocolMessage(x))

class MessageBrokerEventHandler(unknown.UnknownImpl, panorama_projections.IMessageBrokerEventHandler):
    def __init__(self, received_cb : typing.Callable[[Payload], None], published_cb : typing.Callable[[str, str, Payload, bool], None]):
        panorama_projections.IMessageBrokerEventHandler.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "44DEFBD1-8C39-4A1B-9CCF-C5443C79F1C1"

        self.published_cb = published_cb
        self.received_cb = received_cb

    def OnMessageReceived(self, payload):
        if self.received_cb != None:
            py_payload = apidefs.assign(payload, lambda x: Payload(x))
            try:
                self.received_cb(py_payload)
            except Exception as e:
                trace.error(f"Exception raised in message broker message received callback : {e}")

    def OnPublished(self, publisher, message_id, payload, successful):
        if self.published_cb != None:
            py_payload = apidefs.assign(payload, lambda x: Payload(x))
            try:
                self.published_cb(publisher, message_id, py_payload, successful)
            except Exception as e:
                trace.error(f"Exception raised in message broker message received callback : {e}")

class MessageBroker(apidefs.BaseProjection):
    def __init__(self, native):
        apidefs.BaseProjection.__init__(self, native)

    def initialize(self):
        """
        Instructs the message broker to initialize the target protocols and set up the routes between the message-ids and those targets.  Multiple calls is safe, but initialization is only done once.
        """
        apidefs.CHECKHR(self.native_pointer().Initialize())

    def subscribe(self, subscription_id : str, cb: typing.Callable[[Payload], None]):
        """
        Subscribes to messages recieved on the specified subscription id

        Args:
            subscription_id: Id of the subscription specified in the configuration file.  See :ref:`Message Broker Config <message_broker_config>` for more details.
            cb: Callback that is invoked when a message is received

        Returns:
            Integer: Passed to unsubscribe to stop receiving messages from this subscription-id
        """
        handler = MessageBrokerEventHandler(cb, None)
        panorama_projections.PyObjectAddRef(handler)
        res = self.native_pointer().Subscribe(subscription_id, handler)
        apidefs.CHECKHR(res)
        return res

    def unsubscribe(self, cancellation_token : int):
        """
        Unsubscribes from a subscription-id

        Args:
            cancellation_token: Token returned from a call to subscribe
        """
        self.native_pointer().Unsubscribe(cancellation_token)

    def publish(self, message_id : str, payload : Payload):
        """
        Publishes a payload as 'message_id' to the message broker

        Args:
            message_id: The id of the message. See :ref:`Message Broker Config <message_broker_config>` for more details.
            payload: The payload of the message
        """
        apidefs.CHECKHR(self.native_pointer().Publish(message_id, payload.native_pointer()))

    def publish_async(self, message_id : str, payload : Payload, cb : typing.Callable[[str, str, Payload, bool], None]):
        """
        Asynchronously publishes a payload as 'message_id' to the message broker

        Args:
            message_id: The id of the message. See :ref:`Message Broker Config <message_broker_config>` for more details.
            payload: The payload of the message
            cb: Method to invoke on completion of the async publish
        """
        handler = MessageBrokerEventHandler(None, cb)
        panorama_projections.PyObjectAddRef(handler)
        apidefs.CHECKHR(self.native_pointer().PublishAsync(message_id, payload.native_pointer(), handler))

    def add_protocol_factory(self, factory : panorama_projections.IProtocolFactory):
        """
        Adds a python implementation of the IProtocolFactory interface to the message broker

        Args:
            factory:  The protocol factory
        """
        panorama_projections.PyObjectAddRef(factory)
        apidefs.CHECKHR(self.native_pointer().AddProtocolFactory(factory))

    def uuid():
        # Must equal IMessageBroker UUID
        return "7BF68FD8-8D46-4A7A-A1A4-F2922CEA74FE"

    

def create(credential_provider : credentials.CredentialProvider, config = None, unique = False):
    """
    Creates the mqtt message broker.  See :ref:`Creating Message Broker <message_broker_create>` for more details.

    Args:
        credential_provider: Object that provides the AWS credentials for any underlying protocol client that may need it
        config: The message broker configuration
        unique: Will create a unique instance of the message broker, regardless of the configuration
    """
    res = panorama_projections.CreateMessageBroker(credential_provider.native_pointer(), config, unique)
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: MessageBroker(x))

def set_default_config(config : str):
    """
    Sets the default configuration value that will be used for creating message brokers.  No validation is done here.
    """
    panorama_projections.SetMessageBrokerDefaultConfig(config)

def create_empty_batch_payload():
    """
    Creates an empty batch payload

    Returns:
        BatchPayload
    """
    res = panorama_projections.CreateEmptyBatchPayload()
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: BatchPayload(x))

def create_payload_from_string(contents : str):
    """
    Creates a payload from a string

    Args:
        contents: The content of the payload

    Returns:
        Payload
    """
    res = panorama_projections.CreatePayloadFromString(contents)
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: Payload(x))

def create_payload_from_buffer(contents : buffer.Buffer):
    """
    Creates a payload from a buffer

    Args:
        contents: The content of the payload

    Returns:
        Payload
    """
    res = panorama_projections.CreatePayloadFromBuffer(contents.native_pointer())
    apidefs.CHECKHR(res[0])
    return apidefs.attach(res[1], lambda x: Payload(x))


## Below are base Python implementations of the IProtocol interfaces ##

class PyProtocolMessage(unknown.UnknownImpl, panorama_projections.IProtocolMessage):
    """
    Base class for which Python implementations of the IProtocolMessage object should derive
    """
    def __init__(self):
        panorama_projections.IProtocolMessage.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "820312E3-4F2E-4585-8E4C-134180847184"

    def Payload(self):
        try:
            payload = self.payload()
            return [apidefs.S_OK, payload]
        except Exception as e:
            trace.error(f"Error getting the payload {e}")

    @abstractmethod
    def payload(self):
        raise Exception("payload is not implemented")

class PyProtocolClient(unknown.UnknownImpl, panorama_projections.IProtocolClient):
    """
    Base class for which Python implementations of the IProtocolClient object should derive
    """
    def __init__(self):
        panorama_projections.IProtocolClient.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "B3F32887-02D0-4A8F-9E63-9AC7FFC2FB37"
        self._subscriptions = {}

    def Subscribe(self, subscription, eventHandler):
        if subscription is None:
            trace.error("Subscription object is of type None")
            return apidefs.E_INVALIDARG

        if eventHandler is None:
            trace.error("Event Handler object is of type None")
            return apidefs.E_INVALIDARG

        try:
            self.subscribe(subscription)
        except Exception as e:
            trace.error(f"Exception while calling subscribe: {e}")
            return apidefs.E_FAIL

        token = random.getrandbits(32)
        while token in self._subscriptions:
            token = random.getrandbits(32)

        self._subscriptions[token] = [subscription, eventHandler]
        return token

    def Unsubscribe(self, cancellation_token):
        if cancellation_token in self._subscriptions:
            try:
                self.unsubscribe(self._subscriptions[cancellation_token][0])
            except Exception as e:
                trace.error(f"Exception while calling unsubscribe: {e}")
                return apidefs.E_FAIL

            del self._subscriptions[cancellation_token]

        return apidefs.S_OK

    def Publish(self, message):
        try:
            upcast = unknown.Upcast(message)
            if upcast is None:
                trace.error("Could not upcast python object to it's Derived type")
                return apidefs.E_NOINTERFACE

            self.publish(upcast)
        except Exception as e:
            trace.error(f"Error publishing message: {e}")
            return apidefs.E_FAIL

        return apidefs.S_OK

    def PublishAsync(self, message, eventHandler):
        try:
            upcast = unknown.Upcast(message)
            if upcast is None:
                trace.error("Could not upcast python object to it's Derived type")
                return apidefs.E_NOINTERFACE

            self.publish_async(upcast, eventHandler)
        except Exception as e:
            trace.error(f"Error asynchronously publishing: {e}")
            return apidefs.E_FAIL

        return apidefs.S_OK

    def FriendlyName(self):
        self.__base_friendly_name = self.friendly_name()
        return self.__base_friendly_name

    def invoke_message_received(self, payload, predicate : typing.Callable[[panorama_projections.IProtocolSubscription], bool]):
        for token in self._subscriptions:
            if predicate(self._subscriptions[token][0]):
                try:
                    self._subscriptions[token][1].OnMessageReceived(payload.native_pointer())
                except Exception as e:
                    raise Exception(f"Error invoking message received callback: {e}")

    @abstractmethod
    def publish(self, message):
        raise Exception("publish is not implemented")

    @abstractmethod
    def publish_async(self, message, eventHandler):
        raise Exception("PublishAsync is not implemented")

    @abstractmethod
    def Reconnect(self):
        raise Exception("Reconnect is not implemented")
    
    @abstractmethod
    def subscribe(self, subscription):
        raise Exception("subscribe is not implemented")
    
    @abstractmethod
    def unsubscribe(self, subscription):
        raise Exception("unsubscribe is not implemented")

    @abstractmethod
    def friendly_name(self):
        raise Exception("friendly_name is not implemented")

class PyProtocolSubscription(unknown.UnknownImpl, panorama_projections.IProtocolSubscription):
    """
    Base class for which Python implementations of the IProtocolSubscription object should derive
    """
    def __init__(self):
        panorama_projections.IProtocolSubscription.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "BC511613-FF74-47FC-AAB5-8F7AF25B467F"

class PyProtocolFactory(unknown.UnknownImpl, panorama_projections.IProtocolFactory):
    """
    Base class for which Python implementations of the IProtocolFactory object should derive
    """
    def __init__(self):
        panorama_projections.IProtocolFactory.__init__(self)
        unknown.UnknownImpl.__init__(self)
        self.uuid = "A6374A30-5CFD-44C8-8CAC-C79EB62C460D"

    def CreateProtocol(self, creation_options, credential_provider):
        creds = credentials.create_from_native_pointer(credential_provider)

        try:
            jObj = json.loads(creation_options)
            created_protocol = self.create_protocol(jObj, credential_provider)
            return [apidefs.S_OK, created_protocol]
        except Exception as e:
            trace.error(f"Error creating protocol: {e}")
            return [apidefs.E_FAIL, None]

    def ValidateMessageOptions(self, message_options):
        try:
            jObj = json.loads(message_options)
            if self.validate_message_options(jObj) is True:
                return apidefs.S_OK
            return apidefs.E_INVALIDARG
        except Exception as e:
            trace.error(f"Error validating options: {e}")
            return apidefs.E_FAIL

    def CreateMessage(self, payload, message_options):
        try:
            jObj = json.loads(message_options)
            created_message = self.create_message(payload, jObj)
            return [apidefs.S_OK, created_message]
        except Exception as e:
            trace.error(f"Error creating message: {e}")
            return [apidefs.E_FAIL, None]

    def CreateSubscription(self, subscription_options):
        pass

    def ProtocolName(self):
        self.__base_protocol_name = self.protocol_name()
        return self.__base_protocol_name

    @abstractmethod
    def create_protocol(self, creation_options : dict, credential_provider : credentials.CredentialProvider):
        raise Exception("create_protocol is not implemented")
    
    @abstractmethod
    def protocol_name(self):
        raise Exception("protocol_name is not implemented")
    
    @abstractmethod
    def validate_message_options(self, message_options : dict):
        raise Exception("validate message opitions is not implemented")
    
    @abstractmethod
    def create_message(self, payload : Payload, message_options : dict):
        raise Exception("validate message opitions is not implemented")