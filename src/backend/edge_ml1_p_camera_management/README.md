# Camera Station Application for IPC

This application talks to GenICam compatible industrial cameras via GiGE or USB connected to the IPC. It is expected the camera has been configured with the vendors software (i.e. pylon for Basler, VimbaX for Allied Vision) before using this application, and exposure and gain adjustments are currently primitive. This will also provide ability to preview the image that would be sent to the inference pipeline, as well as trigger pipeline via REST call. It also provides a REST call and returns a JSON document containing all detected cameras to the IPC. The preview can either use a preview pipeline, or it can try to talk directly to the camera. If direct does not work, use the preview pipeline feature. See l4v.ini for configuration details. JPEG image type is assumed for all previews.

## Requirements
    - Ubuntu 18.04 or 20.04
    - X86 CPU with NVIDIA GPU or ARM64+NVIDIA (Jetson) or plain ARM64 or X86 (Slow)
    - python 3.11 installed via apt with python3.11-dev and python 3.11-virtual-env

## Installation\
Clear out any old installs:
```
service l4v-station-camera-app stop
rm /etc/systemd/system/l4v-station-camera-app.service
rm -rf /opt/aws/l4v/camera-station
```
from runtime directory, run:

```
./install
```

This will install default configuration and application is a systemd service (l4v-station-camera-app) and run in 
```
/opt/aws/l4v/camera-station    
```

## Logging

Logging is configured via logging.conf file using standard Python logging. Default file is included to log to:
```
/opt/aws/l4v/camera-station/logs
```

## Usage

###get list of cameras

GET call
```
/cameras
```

returns something like this:
```
[
    {
        "id": "Fake_1",
        "model": "Fake",
        "address": "0.0.0.0",
        "physical_id": "Fake_1",
        "protocol": "Fake",
        "serial": "1",
        "vendor": "Aravis"
    },
    {
        "id": "Allied Vision-1AB22C001C90-005N4",
        "model": "ALVIUM 1800 U-500m",
        "address": "USB3",
        "physical_id": "1AB22C001C90",
        "protocol": "USB3Vision",
        "serial": "005N4",
        "vendor": "Allied Vision"
    },
    {
        "id": "Aravis-Fake-GV01",
        "model": "Fake",
        "address": "127.0.0.1",
        "physical_id": "00:00:00:00:00:00",
        "protocol": "GigEVision",
        "serial": "GV01",
        "vendor": "Aravis"
    },
    {
        "id": "Basler-acA1300-30gc-21409043",
        "model": "acA1300-30gc",
        "address": "192.168.8.193",
        "physical_id": "00:30:53:15:80:13",
        "protocol": "GigEVision",
        "serial": "21409043",
        "vendor": "Basler"
    },
    {
        "id": "Basler-acA640-120gm-21379120",
        "model": "acA640-120gm",
        "address": "192.168.8.189",
        "physical_id": "00:30:53:15:0b:30",
        "protocol": "GigEVision",
        "serial": "21379120",
        "vendor": "Basler"
    },
    {
        "id": "Lucid Vision Labs-TRI050S-C-223302553",
        "model": "TRI050S-C",
        "address": "192.168.8.197",
        "physical_id": "1c:0f:af:ae:f3:8c",
        "protocol": "GigEVision",
        "serial": "223302553",
        "vendor": "Lucid Vision Labs"
    }
]

```
###basic preview html wrapper

Use the camera id from above call in subsequent calls that require <cameraid>

```
/?id=<cameraid>
```

### Execute a pipeline

This executes a pipeline and places the image into the image input directory, as specified in l4v.ini config file. GET Call.

```
/cameras/<string:cameraid>/execute-pipeline
```

### Preview Image

This is meant to populate <img src> body so you can include a preview on a page, or tile previews of multiple cameras. GET Call.

```
/cameras/<string:cameraid>/preview

```

## Authentication

Basic authentication is configured in the l4v.ini file in the BasicAuth section.

Use the following python call to to salt and encode passwords:
```
from werkzeug.security import generate_password_hash, check_password_hash
generate_password_hash("hello")
```
For this config file format, you must manually escape any $ characters with an additional $. Default password is 'lookout'


## Digital IO

In the l4v.ini, you can set digital input triggers which will run the pipeline when either a RISING EDGE or FALLING EDGE on the reqested pin is detected

