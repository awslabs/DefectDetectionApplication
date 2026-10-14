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

"""Device-side workflow tuning (quality-prompt-tuning).

Three contained, best-effort side channels that never touch an
Execution's outcome, artifacts or Run_Metadata:

- :mod:`workflow_engine.tuning.sample_export` — uploads, for every
  Anomaly_Mode invocation, the exact images the executor sent plus the
  recorded answer to the Use_Case's Sample_Store (Requirement 2);
- ``backfill`` — the one-shot export of samples derivable from existing
  Run_Artifacts on the enabled transition (Requirement 2.7);
- :mod:`workflow_engine.tuning.job_runner` — the Device_Score_Job runner
  replaying Candidates against the device-local Text_Generation_API
  (Requirements 6.4, 6.9).

Every module here is inert unless the LocalServer component
configuration enables tuning sample export, and imports no cloud SDK at
import time (Requirements 2.6, 11.3).
"""
