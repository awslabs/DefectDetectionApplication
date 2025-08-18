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

import { Alert, Button } from "@cloudscape-design/components";
import { useNavigate } from "react-router-dom";

export default function FeedbackRequiredAlert({ feedbackBtnHref }: { feedbackBtnHref: string }): JSX.Element {

  const navigate = useNavigate();

  return (
    <Alert
      type="error"
      header="Human feedback required"
      data-test-id="human-feedback-required-alert"
      action={(
        <Button
          onClick={(e): void => {
            e.preventDefault();
            navigate(feedbackBtnHref);
          }}
          href={feedbackBtnHref}
          variant="normal"
        >
          Update human feedback
        </Button>
      )}
    >
      The model's confidence for this prediction is low and requires human feedback.
    </Alert>
  )
}