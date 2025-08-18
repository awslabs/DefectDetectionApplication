Overview
--------

TODO: Brief description of the Panorama-SDK

Installation
------------

**Supported Platforms**

Any combinaton of OS, Architecture, and Python version

-  OS:

   -  Ubuntu 18.04
   -  Ubuntu 20.04

-  Architecture:

   -  x86_64
   -  arm64

-  Python Version:

   -  3.9.16

**Prerequisites**:

.. code-block:: bash

   apt-get update
   apt-get install curl lsb-release python3-pip libgstreamer1.0-dev libgstreamer-plugins-base1.0-dev libgstreamer-plugins-bad1.0-dev gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad gstreamer1.0-plugins-ugly gstreamer1.0-libav gstreamer1.0-tools gstreamer1.0-x gstreamer1.0-alsa gstreamer1.0-gl -y --fix-missing

.. code-block:: bash

   python3 -m pip install Cython numpy boto3 awscrt

The Panorama-SDK uses the aws cpp sdk.  Commands below will fetch and install the pre-compiled aws sdk libraries for the appropriate platform.  The modules compiled in the below packages are: s3;sts;secretsmanager;sagemaker-edge;transfer;iot;iot-data.  If you need to add additional modules see :ref:`additional instructions <custom_aws_sdk_cpp>` on how to build the aws-sdk-cpp for use with the Panorama-SDK.

..  code-block:: bash
   
   aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/$(uname -m)/$(lsb_release -r | awk '{print $2}')/aws-c-iot.deb ./
   aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/$(uname -m)/$(lsb_release -r | awk '{print $2}')/aws-crt-cpp.deb ./
   aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/$(uname -m)/$(lsb_release -r | awk '{print $2}')/aws-iot-device-sdk-cpp-v2.deb ./
   aws s3 cp s3://panorama-sdk-v2-artifacts/dependencies/$(uname -m)/$(lsb_release -r | awk '{print $2}')/aws-sdk-cpp.deb ./

   dpkg -i aws-c-iot.deb
   dpkg -i aws-crt-cpp.deb
   dpkg -i aws-iot-device-sdk-cpp-v2.deb
   dpkg -i aws-sdk-cpp.deb

**Panorama-SDK Packages**:

Panorama-SDK is distributed as a Debian package and Python wheel.  If you are developing in C++ then you only need the Debian package.  If you wish to use Python then you will need both the Debian package and the Python wheel.  To retrieve these files

..  code-block:: bash
   :caption: Debian Package
   
   aws s3 cp s3://panorama-sdk-v2-artifacts/release/latest/$(uname -m)/$(lsb_release -r | awk '{print $2}')/$(python3 --version 2>&1 | awk '{print $2}')/PanoramaSDK.deb ./
   dpkg -i PanoramaSDK.deb

..  code-block:: bash
   :caption: Python Wheel

   aws s3 cp s3://panorama-sdk-v2-artifacts/release/latest/$(uname -m)/$(lsb_release -r | awk '{print $2}')/$(python3 --version 2>&1 | awk '{print $2}')/panorama-1.0-py3-none-any.whl ./
   python3 -m pip install panorama-1.0-py3-none-any.whl

Components
----------

.. toctree::
   :maxdepth: 1

   components/gst_application
   components/gst/gst
   components/properties
   components/message_broker/message_broker
   components/plugins/plugins