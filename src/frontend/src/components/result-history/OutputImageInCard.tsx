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

import { Button } from "@cloudscape-design/components";
import { captureImageType } from "components/live-result/types";
import { APIList } from "config/Interface";
import { useNavigate } from "react-router-dom";
import styled from "styled-components";
import { HistoryResultPageType, InferenceResultHistory } from "./types";
import useAuth from "components/auth/authHook";
import { isInferenceResultPage } from "./utils";

interface OutputImageInCardProps {
  workflowId: string;
  captureId: string;
  resultsList: InferenceResultHistory[];
  historyResultPageType: HistoryResultPageType;
}

const ButtonWithPadding = styled(Button)`
  padding: 6px 22px !important;
`;

export default function OutputImageInCard({
  workflowId,
  captureId,
  resultsList,
  historyResultPageType,
}: OutputImageInCardProps): JSX.Element {

  const { token, authEnabled } = useAuth();

  const getCaptureAPI = APIList.getCapture
    .replace("{workflow_id}", workflowId)
    .replace("{capture_id}", captureId);

  const isInferenceResultPageType = isInferenceResultPage(historyResultPageType);

  const getImageSrc = (): string => {
    return `${getCaptureAPI}/${isInferenceResultPageType ? captureImageType.OUTPUT_IMAGE : captureImageType.INPUT_IMAGE}${authEnabled ? `?token=${encodeURIComponent(token)}` : ""}`;
  };
  const imageSrc = getImageSrc();
  const navigate = useNavigate();
  const resultDetailsUrl = `/history/${workflowId}/detail/${captureId}`;
  const captureResultDetailsUrl = `/capture-results/${workflowId}/detail/${captureId}`;

  return (
    <div style={{ width: "100%", height: "100%", position: "relative" }}>
      <img
        src={imageSrc}
        alt="SourceImage"
        style={{ width: "100%", height: "100%", position: "relative" }}
      ></img>
      <div style={{ position: "absolute", bottom: "12px", right: "12px" }}>
        <ButtonWithPadding
          iconName="expand"
          onClick={(): void =>
            navigate(isInferenceResultPageType ? resultDetailsUrl : captureResultDetailsUrl, { state: resultsList })
          }
        />
      </div>
    </div>
  );
}
