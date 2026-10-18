"""The Results_Bucket of a use case and the three S3 probes of the debug API
(workflow-debug-mode design §3, §7.2; tasks.md decision P4).

- ``results_bucket(usecase)`` resolves the bucket as ``captures.py`` does: the
  use case's ``inference_uploader_s3_bucket`` override, else
  ``dda-inference-results-<account_id>``.
- ``probe_region`` (decision P4): the design routes the region probe through
  the ``media-retention-read`` credential path, whose session policy and
  API-role grants carry no ``s3:ListBucket``, so the probe would be denied in
  both topologies. It therefore runs ``ListObjectsV2(Prefix='dda-telemetry/debug/<usecase_id>/',
  MaxKeys=1)`` through the ``image-read`` path, whose ``s3:ListBucket`` is
  conditioned on exactly that prefix, reads the ``x-amz-bucket-region`` header
  first (also from a redirect error's headers) and falls back to the use
  case's region only when the header is absent, reporting ``region_source``.
- ``images_ready`` reads the bucket's lifecycle configuration through the
  ``media-retention-read`` path and requires the ``DDATelemetryDebugImages``
  rule (``telemetry.retention.debug_images_rule_present``).
- ``head_image`` / ``presign_image`` serve the image route through the
  ``image-read`` path (``HeadObject``, then a 120 s presigned GET).
"""
import logging
from collections import namedtuple
from typing import Any, Dict, Optional

from botocore.exceptions import BotoCoreError, ClientError

import shared_utils
from edge_telemetry_common import credentials
from telemetry import retention

__all__ = ["DEBUG_IMAGE_PREFIX", "PRESIGN_SECONDS", "RegionProbe", "ImagesReadiness", "results_bucket",
           "probe_region", "images_ready", "head_image", "presign_image"]

logger = logging.getLogger(__name__)

DEBUG_IMAGE_PREFIX = "dda-telemetry/debug/"
PRESIGN_SECONDS = 120
REGION_HEADER = "x-amz-bucket-region"

RegionProbe = namedtuple("RegionProbe", ["region", "region_source"])
ImagesReadiness = namedtuple("ImagesReadiness", ["ready", "reason", "rules"])


def results_bucket(usecase: Dict[str, Any]) -> str:
    account_id = str(usecase.get("account_id") or "")
    return str(usecase.get("inference_uploader_s3_bucket") or "dda-inference-results-{0}".format(account_id))


def _header_region(response: Any) -> Optional[str]:
    headers = ((response or {}).get("ResponseMetadata") or {}).get("HTTPHeaders") or {}
    for key, value in headers.items():
        if str(key).lower() == REGION_HEADER and value:
            return str(value)
    return None


def probe_region(usecase: Dict[str, Any], bucket: str, *, client: Any = None) -> RegionProbe:
    """``(region, region_source)``: ``header`` when S3 reported the bucket's
    region, ``usecase`` when the header was absent or the probe failed."""
    fallback = shared_utils.get_usecase_region(usecase)
    prefix = DEBUG_IMAGE_PREFIX + str(usecase["usecase_id"]) + "/"
    try:
        s3 = client if client is not None else credentials.client("s3", usecase, "image-read", bucket=bucket)
        response = s3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1)
        region = _header_region(response)
    except ClientError as error:
        region = _header_region(error.response)
        if region is None:
            logger.warning("results bucket region probe failed for use case %s: %s",
                           usecase.get("usecase_id"), (error.response.get("Error") or {}).get("Code"))
    except (BotoCoreError, KeyError, ValueError) as error:
        logger.warning("results bucket region probe failed for use case %s: %s",
                       usecase.get("usecase_id"), error.__class__.__name__)
        region = None
    if region:
        return RegionProbe(region, "header")
    return RegionProbe(fallback, "usecase")


def images_ready(usecase: Dict[str, Any], bucket: str, *, client: Any = None) -> ImagesReadiness:
    """Whether the bucket carries the enabled ``DDATelemetryDebugImages`` rule."""
    try:
        s3 = client if client is not None else credentials.client("s3", usecase, "media-retention-read", bucket=bucket)
        response = s3.get_bucket_lifecycle_configuration(Bucket=bucket)
        rules = list(response.get("Rules") or [])
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        if code == "NoSuchLifecycleConfiguration":
            return ImagesReadiness(False, "rule_absent", [])
        logger.warning("lifecycle read failed for use case %s: %s", usecase.get("usecase_id"), code)
        return ImagesReadiness(False, "lifecycle_unreadable", [])
    except BotoCoreError as error:
        logger.warning("lifecycle read failed for use case %s: %s", usecase.get("usecase_id"), error.__class__.__name__)
        return ImagesReadiness(False, "lifecycle_unreadable", [])
    if retention.debug_images_rule_present(rules):
        return ImagesReadiness(True, None, rules)
    return ImagesReadiness(False, "rule_absent", rules)


def head_image(usecase: Dict[str, Any], bucket: str, key: str, *, client: Any = None) -> bool:
    """True when the object exists (``HeadObject``); False on 404 or 403."""
    s3 = client if client is not None else credentials.client("s3", usecase, "image-read", bucket=bucket)
    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as error:
        code = str((error.response.get("Error") or {}).get("Code") or "")
        if code in ("404", "NoSuchKey", "NotFound", "403", "AccessDenied", "Forbidden"):
            return False
        raise


def presign_image(usecase: Dict[str, Any], bucket: str, key: str, *, client: Any = None,
                  expires_in: int = PRESIGN_SECONDS) -> str:
    s3 = client if client is not None else credentials.client("s3", usecase, "image-read", bucket=bucket)
    return s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": key}, ExpiresIn=int(expires_in))
