"""
Route wiring and structural example tests for the Portal_Pin_API
(cloud-static-camera-provisioning task 2.8).

- Route wiring: unknown static-image sub-routes 404, OPTIONS/CORS,
  malformed JSON bodies, missing/invalid staging key, missing fileName,
  missing staged object, upload-url response shape.
- Req 1.6 (structural): each Pin_Request is associated with exactly one
  Target_Device — the item lives in that device's partition and the
  desired document is written only to that thing.
- Req 6.5: the generic Camera_Registry mutation routes reject csid
  ``static-image-camera`` (a device-reported discovery-managed entry)
  with the existing DISCOVERY_MANAGED rejection, leaving the entry
  unchanged.

_Requirements: 1.6, 6.5_
"""
import pytest

from pin_route_helpers import (  # noqa: F401 — pin_env fixture import
    FakeIotDataClient, RecordingS3, create_usecase, image_bytes, invoke,
    make_user, pin_env, pin_request_items, register_device, stage_object,
    submit_pin,
)


@pytest.fixture(scope="module")
def operator(pin_env):
    usecase_id = create_usecase(pin_env)
    return make_user("Operator"), usecase_id


@pytest.fixture()
def fakes(pin_env):
    fake_s3 = RecordingS3(pin_env.s3)
    fake_shadow = FakeIotDataClient()
    pin_env.s3_holder["client"] = fake_s3
    pin_env.shadow_holder["client"] = fake_shadow
    return fake_s3, fake_shadow


# ---------------------------------------------------------------------------
# Route wiring
# ---------------------------------------------------------------------------

def test_options_returns_cors_preflight(pin_env, operator):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    response = pin_env.module.handler({
        "httpMethod": "OPTIONS",
        "path": f"/devices/{device_id}/cameras/static-image/pin",
        "pathParameters": {"id": device_id},
    }, None)
    assert response["statusCode"] == 200
    assert "Access-Control-Allow-Methods" in response["headers"]


def test_unknown_static_image_sub_route_is_404(pin_env, operator, fakes):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    status, body = invoke(pin_env, "GET", device_id, user,
                          sub_path="/static-image/unknown")
    assert status == 404
    # PUT is not a supported method on the pin resource.
    status, body = invoke(pin_env, "PUT", device_id, user,
                          sub_path="/static-image/pin")
    assert status == 404


def test_malformed_json_body_is_400(pin_env, operator, fakes):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    status, body = invoke(pin_env, "POST", device_id, user,
                          sub_path="/static-image/pin",
                          raw_body="{not json")
    assert status == 400
    assert body["error"] == "Invalid JSON body"


def test_missing_body_and_missing_staging_key_are_400(pin_env, operator,
                                                      fakes):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    # No body at all.
    status, body = invoke(pin_env, "POST", device_id, user,
                          sub_path="/static-image/pin")
    assert status == 400
    # Body without stagingKey.
    status, body = invoke(pin_env, "POST", device_id, user,
                          sub_path="/static-image/pin",
                          body={"fileName": "a.png"})
    assert status == 400
    assert "stagingKey" in body["error"]


def test_staging_key_outside_staging_prefix_is_400(pin_env, operator, fakes):
    """Clients cannot point the pin route at arbitrary bucket objects."""
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    status, body = invoke(
        pin_env, "POST", device_id, user, sub_path="/static-image/pin",
        body={"stagingKey": "artifacts/other-object", "fileName": "a.png"})
    assert status == 400
    assert "stagingKey" in body["error"]


def test_missing_file_name_is_400(pin_env, operator, fakes):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    staging_key = stage_object(pin_env, image_bytes())
    status, body = invoke(pin_env, "POST", device_id, user,
                          sub_path="/static-image/pin",
                          body={"stagingKey": staging_key})
    assert status == 400
    assert "fileName" in body["error"]


def test_missing_staged_object_is_400(pin_env, operator, fakes):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    status, body = invoke(
        pin_env, "POST", device_id, user, sub_path="/static-image/pin",
        body={"stagingKey": "static-image-pins/staging/never-uploaded",
              "fileName": "a.png"})
    assert status == 400
    assert "Staged upload not found" in body["error"]
    fake_s3, fake_shadow = fakes
    assert pin_request_items(pin_env, device_id) == []
    assert fake_s3.copies == []
    assert fake_shadow.updates == []


def test_upload_url_response_shape(pin_env, operator, fakes):
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    status, body = invoke(pin_env, "POST", device_id, user,
                          sub_path="/static-image/upload-url")
    assert status == 200
    assert body["deviceId"] == device_id
    assert body["bucket"] == pin_env.bucket
    assert body["stagingKey"].startswith("static-image-pins/staging/")
    assert body["expiresInSeconds"] == 15 * 60
    assert body["stagingKey"] in body["uploadUrl"]


def test_status_route_returns_no_request_response(pin_env, operator, fakes):
    """A registered device with zero Pin_Requests gets a response, not an
    error (Reqs 1.10, 4.7)."""
    user, usecase_id = operator
    device_id = register_device(pin_env, usecase_id)
    status, body = invoke(pin_env, "GET", device_id, user,
                          sub_path="/static-image")
    assert status == 200
    assert body["deviceId"] == device_id
    assert body["latest"] is None
    assert body["noPinRequest"] is True


# ---------------------------------------------------------------------------
# Req 1.6: exactly one Target_Device per Pin_Request (structural)
# ---------------------------------------------------------------------------

def test_pin_request_is_associated_with_exactly_one_device(pin_env,
                                                           operator, fakes):
    user, usecase_id = operator
    fake_s3, fake_shadow = fakes
    device_a = register_device(pin_env, usecase_id)
    device_b = register_device(pin_env, usecase_id)

    status_a, body_a = submit_pin(pin_env, device_a, user, image_bytes())
    status_b, body_b = submit_pin(pin_env, device_b, user, image_bytes())
    assert status_a == 201 and status_b == 201
    assert body_a["deviceId"] == device_a
    assert body_b["deviceId"] == device_b
    assert body_a["pinRequestId"] != body_b["pinRequestId"]

    # Each item lives only in its own device's partition...
    items_a = pin_request_items(pin_env, device_a)
    items_b = pin_request_items(pin_env, device_b)
    assert [i["pin_request_id"] for i in items_a] == [body_a["pinRequestId"]]
    assert [i["pin_request_id"] for i in items_b] == [body_b["pinRequestId"]]

    # ...and each desired document went only to its own thing.
    by_thing = {u["thing_name"]: u for u in fake_shadow.updates}
    assert set(by_thing) == {device_a, device_b}
    assert by_thing[device_a]["payload"]["state"]["desired"][
        "staticImagePin"]["requestId"] == body_a["pinRequestId"]
    assert by_thing[device_b]["payload"]["state"]["desired"][
        "staticImagePin"]["requestId"] == body_b["pinRequestId"]


# ---------------------------------------------------------------------------
# Req 6.5: discovery-managed rejection of the static camera entry
# ---------------------------------------------------------------------------

def _put_static_camera_entry(pin_env, device_id, usecase_id):
    item = {
        "device_id": device_id,
        "sk": "CAMERA#static-image-camera",
        "camera_source_id": "static-image-camera",
        "usecase_id": usecase_id,
        "name": "Static Image Camera",
        "type": "StaticImage",
        "params": {},
        "capabilities": {"staticImage": {"width": 64, "height": 48}},
        "origin": "edge-discovered",
        "version": 3,
        "sync_status": "synced",
        "absent": False,
        "last_reported_at": 1_700_000_000_000,
    }
    pin_env.registry.put_item(Item=item)
    return item


def test_generic_mutation_routes_reject_static_camera_entry(pin_env,
                                                            operator,
                                                            fakes):
    """The device-reported static-image-camera entry is discovery-managed:
    generic PUT/DELETE mutations get the existing DISCOVERY_MANAGED
    rejection and the entry is unchanged (Req 6.5)."""
    user, usecase_id = operator
    fake_s3, fake_shadow = fakes
    device_id = register_device(pin_env, usecase_id)
    before = _put_static_camera_entry(pin_env, device_id, usecase_id)

    status, body = invoke(
        pin_env, "PUT", device_id, user,
        sub_path="/static-image-camera", csid="static-image-camera",
        body={"name": "Renamed", "type": "StaticImage", "params": {}})
    assert status == 409
    assert body["code"] == "DISCOVERY_MANAGED"
    assert body["camera_source_id"] == "static-image-camera"
    assert "discovery-managed" in body["error"]

    status, body = invoke(
        pin_env, "DELETE", device_id, user,
        sub_path="/static-image-camera", csid="static-image-camera")
    assert status == 409
    assert body["code"] == "DISCOVERY_MANAGED"

    after = pin_env.registry.get_item(
        Key={"device_id": device_id,
             "sk": "CAMERA#static-image-camera"})["Item"]
    assert after == before
    assert fake_shadow.updates == []
