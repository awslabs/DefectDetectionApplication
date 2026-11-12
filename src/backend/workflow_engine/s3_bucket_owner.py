# Copyright 2025 Amazon Web Services, Inc.
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
"""``ExpectedBucketOwner`` for the device-side writers of the DERIVED
inference-results bucket (S3 bucket-squatting fix).

The Portal names a use case's default results bucket from the use case's
account id, ``dda-inference-results-<account>``, and hands that name to the
device through the InferenceUploader configuration, the telemetry live
lease, the debug session entry and the tuning ``workflowTuning``
configuration. Nothing creates the bucket for the operator, and S3 bucket
names are global, so a third party who knows the account id can create that
exact name first; the device would then upload inference images, live
frames, debug images and tuning samples into the attacker's bucket and read
tuning manifests from it.

A bucket of that derived shape must belong to the account its name encodes.
Every device-side S3 call against such a name asserts that account as
``ExpectedBucketOwner``; S3 answers 403 when any other account owns the
bucket, and the write (or read) fails instead of being served. A bucket of
any other name (an administrator's override, admitted by the Portal's bucket
allowlist) carries no assertion: its owner is not encoded anywhere the
device can trust.
"""
import re
from typing import Any, Dict, Optional

__all__ = ["RESULTS_BUCKET_PATTERN", "derived_bucket_owner", "owner_args"]

#: ``dda-inference-results-<12-digit account id>``: the only derived name
#: the device writes to.
RESULTS_BUCKET_PATTERN = re.compile(r"^dda-inference-results-(\d{12})$")


def derived_bucket_owner(bucket: Any) -> Optional[str]:
    """The account id a derived ``dda-inference-results-<account>`` name
    encodes, else ``None``."""
    match = RESULTS_BUCKET_PATTERN.match(str(bucket or ""))
    return match.group(1) if match else None


def owner_args(bucket: Any) -> Dict[str, str]:
    """``{"ExpectedBucketOwner": <account>}`` for a derived results bucket
    name, else ``{}``; for splatting into any boto3 S3 call (or into the
    ``ExtraArgs`` of a managed transfer)."""
    owner = derived_bucket_owner(bucket)
    return {"ExpectedBucketOwner": owner} if owner else {}
