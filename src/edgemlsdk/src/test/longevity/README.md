 ## Longevity Tests
 EdgeML-SDK Longevity Tests evaluates features by running end to end tests for an extended period of time and measures metrics related to CPU usage, memory consumption etc.. 
 
 ### Prerequisites
 - install aws cli 
 - install python3 and python3-pip
 - install boto3
 - paste admin/ReadOnly credentials for account `691462484548` on terminal
 ### Run Longevity Tests
 
 ```
 usage: deploy.py [-h] [--platform PLATFORM] [--ubuntu-version UBUNTU_VERSION] [--python-version PYTHON_VERSION] [--release-date RELEASE_DATE] [--longevity-hrs LONGEVITY_HRS] {mqtt} ...
 ```
 
 ### Mqtt Message Broker Longevity Tests
 To run mqtt message broker longevity tests 
 ```
 python3 deploy.py [--platform PLATFORM] [--ubuntu-version UBUNTU_VERSION] [--python-version PYTHON_VERSION] [--release-date RELEASE_DATE] [--longevity-hrs LONGEVITY_HRS] mqtt [--publish-freq PUBLISH_FREQ] [--region REGION] [--mqtt_endpoint MQTT_ENDPOINT]
 ```
 Check log group `edgemlsdk-<ubuntu_version>-<platform>-<python_version>-<longevity_type>` in cloudwatch to monitor logs for currently running longevity tests.
 for e.g. `edgemlsdk-18.04-aarch64-3.9-mqtt`
 
 ### Adding More Longevity Tests
 - To add longevity tests for other features in edgeml-sdk 
   - Create a python script and launch script similar to `mqtt/mqtt_longevity.py` and `mqtt/run_mqtt_longevity.sh` respectively.
   - Edit the `deploy.py` script to add longevity tests to run similar to mqtt longevity