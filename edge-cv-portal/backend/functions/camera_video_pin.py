"""
Portal_Video_Pin_API Lambda entry point (static-camera-video-loop, design
Decision 8).

The CameraVideoPinHandler function serves only the four static-video routes
under /devices/{id}/cameras:

    POST   /devices/{id}/cameras/static-video/upload-url   (MANAGE_DEVICES)
    POST   /devices/{id}/cameras/static-video/pin          (MANAGE_DEVICES)
    DELETE /devices/{id}/cameras/static-video/pin          (MANAGE_DEVICES)
    GET    /devices/{id}/cameras/static-video              (VIEW_DEVICES)

plus the asynchronous Video_Validation job the pin route hands to it
(``{"videoValidation": {"deviceId", "validationId"}}``, an Event
invocation of this function by its fixed name).

The route handlers and the job live in camera_registry.py next to the image
pin routes (same code asset, same helpers); this module only narrows the
function to its routes and dispatches the job. It exists as its own
function because Video_Validation needs the video layer (OpenCV, about
190 MB), which the CameraRegistryHandler and its routes do not carry.
"""
import camera_registry

#: Path suffixes of the routes this function serves.
VIDEO_ROUTE_SUFFIXES = (
    '/cameras/static-video',
    '/cameras/static-video/upload-url',
    '/cameras/static-video/pin',
)


def handler(event, context):
    """Run a validation job, or dispatch a static-video route to
    camera_registry; anything else is a 404 (API Gateway only routes the
    static-video paths here)."""
    event = event or {}
    if camera_registry.VIDEO_VALIDATION_EVENT_KEY in event:
        return camera_registry.run_video_validation(
            event[camera_registry.VIDEO_VALIDATION_EVENT_KEY])
    path = event.get('path', '') or ''
    if event.get('httpMethod') != 'OPTIONS' and \
            not path.endswith(VIDEO_ROUTE_SUFFIXES):
        return camera_registry.create_response(404, {'error': 'Not found'})
    return camera_registry.handler(event, context)
