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

import {
  Box,
  Button,
  Container,
  ContentLayout,
  Header,
  SpaceBetween,
} from "@cloudscape-design/components";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import ResultDetailsCardDisplay from "./ResultDetailsCardDisplay";
import { HistoryResultPageType, InferenceResultHistory } from "./types";
import React, { useState } from "react";
import ResultDetailsNote from "./ResultDetailsNote";
import { useMutation } from "@tanstack/react-query";
import { updateInferenceResults } from "api/InferenceResultAPI";
import { AppLayoutContext } from "components/layout/AppLayoutContext";

export default function ResultDetails({ pageType }: { pageType: HistoryResultPageType }): JSX.Element {
  const workflowId = useParams().workflowId ?? "";
  const captureId = useParams().captureId ?? "";
  // TODO: current prev/next functionality only work with current page data (data cached in navigate state when user navigates from result list page to detail page)
  const { state } = useLocation();
  const [cachedHistoryList, setCachedHistoryList] = useState<InferenceResultHistory[]>(state);
  const [isEditMode, setIsEditMode] = useState(false);
  const startIndex = cachedHistoryList
    .map((res: InferenceResultHistory) => res.captureId)
    .indexOf(captureId);
  const numOfImages = cachedHistoryList.length;
  const [index, setIndex] = React.useState(startIndex);
  const { addError } = React.useContext(AppLayoutContext);
  const navigate = useNavigate();

  const curResult = cachedHistoryList[index];
  const { captureId: curCaptureId, textNote: curTextNote = "" } = curResult;

  const {
    mutate: updateTextNote,
    isLoading: isUpdatingTextNote,
  } = useMutation({
    mutationFn: (textNote: string) =>
      updateInferenceResults(workflowId, [curCaptureId], { textNote }),
    onSuccess: (_, textNote) => {
      const targetItem = cachedHistoryList.find(item => item.captureId === curCaptureId);
      if (!!targetItem) {
        targetItem.textNote = textNote;
      }
      setCachedHistoryList([...cachedHistoryList]);
      setIsEditMode(false);
      // update the useNavigate state as well, otherwise the updated data won't be apply after page refresh
      navigate("#", { state: cachedHistoryList });
    },
    onError: (_) => {
      addError({
        header: "Failed to save edited notes",
        content: "Unable to save the edited notes."
      });
    },
  });

  return (
    <ContentLayout header={<Header variant="h1">Result details</Header>}>
      <SpaceBetween direction="vertical" size="xs">
        <Container
          header={
            <Header
              variant="h2"
              actions={
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    disabled={index - 1 < 0}
                    onClick={(): void => setIndex(index - 1)}
                  >
                    Previous image
                  </Button>
                  <Button
                    disabled={index + 1 >= numOfImages}
                    onClick={(): void => setIndex(index + 1)}
                  >
                    Next image
                  </Button>
                </SpaceBetween>
              }
            >
              {curCaptureId}
            </Header>
          }
        >
          <Box padding={{ top: "s" }}>
            {!!captureId && (
              <ResultDetailsCardDisplay
                workflowId={workflowId}
                captureId={curCaptureId}
                inferenceResult={curResult}
                historyResultPageType={pageType}
              />
            )}
          </Box>
        </Container>
        <ResultDetailsNote
          initialNotes={curTextNote || ""}
          isEditMode={isEditMode}
          onChangeEditMode={setIsEditMode}
          onSave={updateTextNote}
          isUpdating={isUpdatingTextNote}
        />
      </SpaceBetween>
    </ContentLayout>
  );
}
