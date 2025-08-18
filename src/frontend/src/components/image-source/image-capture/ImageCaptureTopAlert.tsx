/*
 *
 * Copyright 2025 Amazon Web Services, Inc.
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 *  You may obtain a copy of the License at
 *
 *      http://www.apache.org/licenses/LICENSE-2.0
 *
 *  Unless required by applicable law or agreed to in writing, software
 *  distributed under the License is distributed on an "AS IS" BASIS,
 *  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 *
 */

import { Alert } from "@cloudscape-design/components"
import { WorkflowTriggerType } from "../types"

interface ImageCaptureTopAlertProps {
  hasModel: Boolean,
  workflowTriggerType: WorkflowTriggerType | null,
}

export default function ImageCaptureTopAlert({
  hasModel,
  workflowTriggerType,
}: ImageCaptureTopAlertProps): JSX.Element {
  if (workflowTriggerType === WorkflowTriggerType.DigitalInput) {
    return (
      <Alert type="info">
        Selected workflow uses digital input trigger. Image capture initiation from the UI is disabled.
      </Alert>
    )
  }
  if (hasModel) {
    return (
      <Alert type="info">
        Selected workflow uses a model. Only single image capture is supported. Capture interval and prefix are not supported. To capture multiple images, use a workflow without a model.
      </Alert>
    )
  }
  return <></>
}