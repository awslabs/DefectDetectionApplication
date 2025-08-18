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

from panorama import credentials
from abc import ABC, abstractmethod

import boto3
import botocore.credentials
import botocore.session
from boto3.session import Session
from awscrt import auth

# import threading
import json

from panorama import panorama_projections
from panorama import trace
from panorama import properties
from panorama import apidefs

class App(properties.PropertyDelegate, credentials.CredentialProvider):
    METHOD = "PanoramaDeviceCredentialProvider"

    def __init__(self, native):
        properties.PropertyDelegate.__init__(self, native)
        self.cred_provider = None

    def add_property_delegate(self, delegate: properties.PropertyDelegate):
        apidefs.CHECKHR(self.native_pointer().AddPropertyDelegate(delegate._native))

    def create_boto3_session(self):
        """
        Creates a boto3.Session that will load and refresh AWS credentials
        provided by the Panorama device

        Example:
        session = app.create_boto3_session()
        s3client = session.client('s3')

        :return: A boto3 session from which you can create your aws client
        :rtype: :class:`botocore.session.Session`
        """
        session = botocore.session.get_session()
        boto3_session = Session(botocore_session=session)

        if self.cred_provider == None:
            self.cred_provider = session.get_component('credential_provider')
            self.cred_provider.insert_before('env', self)

        return boto3_session

    def load(self):
        """
        Implementation of botocore.credentials.CredentialProvider.load that creates
        a refreshable credential that will query the MDS to refresh the AWS credentials.

        :return: Credentials vended from host environment.  See :ref:`Credential Provider <credential-provider>` for more information
        :rtype: botocore.credentials.Credentials
        """
        trace.verbose("Loading Credentials")
        metadata = self.get_credentials()
        if metadata.get('expiry_time') != None:
            return botocore.credentials.RefreshableCredentials.create_from_metadata(
                metadata,
                self.get_credentials,
                self.METHOD
            )

        trace.warning("Returned credentials did not contain an expiry time, so they are not refreshable")
        return botocore.credentials.Credentials(
            access_key=metadata['access_key'],
            secret_key=metadata['secret_key'],
            token=metadata.get('token'),
            method=self.METHOD,
        )

    def get_credentials(self):
        """
        Gets credentials as a JSON string

        :rtype: json object holding the credentials.  {'access_key': '...', 'secret_key': '...', 'token': '...', 'expiry_time': '...'}
        """
        trace.verbose("Refreshing Credentials")
        creds = self.native_pointer().GetCredentialsAsJSON()
        if creds[0] != 0:
            trace.error(f"Failed to get credentials from Panorama device: {creds[0]}")
            raise botocore.credentials.CredentialRetrievalError(
                provider=self.METHOD,
                error_msg=f"Failed to get credentials from Panorama device: {creds[0]}"
            )

        try:
            metadata = json.loads(creds[1].AsString())
            creds[1].Release()
            return {
                "access_key": metadata["access_key"],
                "secret_key": metadata["secret_key"],
                "token": metadata["token"],
                "expiry_time": metadata["expiry_time"]
            }
        except Exception as e:
            trace.error(f"Failed to parse credentials from device: {e}")
            raise botocore.credentials.CredentialRetrievalError(
                provider=self.METHOD,
                error_msg=f"Failed to parse credentials from device: {e}"
            )

    def _get_credentials_callback(self):
        trace.info(f'Refreshing credentials for the provider')
        creds = self.get_credentials()
        expiry_date = botocore.credentials.RefreshableCredentials._expiry_datetime(creds['expiry_time'])
        return auth.AwsCredentials(creds['access_key'], creds['secret_key'], creds['token'], expiry_date)

    def aws_credentials_provider(self):
        """
        Some APIs for AWS services, like MQTT, make use of a :class:`auth.AwsCredentialsProvider` object instead of a boto3 session for authentication.  
        This method will generate this object and query the host system for the appropriate credentials.  See :ref:`Credential Provider <credential-provider>` for more information

        :return: AwsCredentialsProvider
        """
        return auth.AwsCredentialsProvider.new_delegate(
                   get_credentials=self._get_credentials_callback)

def create(args = None):
    """
    Create an instance of IApp that is designed for applications intended to run on the Panorama device (i.e. Connect to the MDS service)
    If you are planning to deploy to GGv2 see <TODO>
    """
    res = None
    if args != None:
        res = panorama_projections.CreatePanoramaApp(len(args) + 1, [b''] + args)
    else:
        res = panorama_projections.CreatePanoramaApp(0, None)

    apidefs.CHECKHR(res[0])
    return App(res[1])