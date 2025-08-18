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

import { Alert, Button, DateRangePicker, DateRangePickerProps, Form, FormField, Input, Modal, Select, SpaceBetween, TextContent } from "@cloudscape-design/components";
import { useEffect, useState } from "react";
import { css } from "@emotion/css";
import { PredictionResult, retrainInputImages } from "api/WorkflowAPI";
import { OptionDefinition } from "@cloudscape-design/components/internal/components/option/interfaces";
import useAuth from "components/auth/authHook";

interface DownloadResultModalProps {
  visible: boolean;
  onClose: () => void;
  workflowId: string;
}

const MAX_QUANTITY = 1000;
const MIN_QUANTITY = 1;

const imageClassificationOptions: OptionDefinition[] = [
  {
    label: "Normal and anomaly results",
    value: PredictionResult.ALL
  },
  {
    label: "Normal results",
    value: PredictionResult.NORMAL
  },
  {
    label: "Anomaly results",
    value: PredictionResult.ANOMALY
  }
]

const getTimestampInSeconds = (timestampInMs: number): number => Math.floor(timestampInMs / 1000);

export default function DownloadResultModal({
  visible,
  onClose,
  workflowId,
}: DownloadResultModalProps): JSX.Element {
  const { token, authEnabled } = useAuth();
  const [dateRange, setTimeRange] = useState<DateRangePickerProps.Value | null>(null);
  const [imageQuantity, setImageQuantity] = useState(`${MIN_QUANTITY}`);
  const [selectedOption, setSelectedOption] = useState(imageClassificationOptions[0]);
  const [showErrorAlert, setShowErrorAlert] = useState(NaN);
  const [isDownloading, setIsDownloading] = useState(false);
  const isReadyToDownload = !!dateRange && Number.parseInt(imageQuantity) > 0 && !!selectedOption;

  const cleanup = (): void => {
    setTimeRange(null);
    setImageQuantity(`${MIN_QUANTITY}`);
    setSelectedOption(imageClassificationOptions[0]);
    setShowErrorAlert(NaN);
    setIsDownloading(false);
  }

  useEffect(() => {
    if (!visible) {
      cleanup();
    }
  }, [visible])

  return (
    <Modal
      visible={visible}
      onDismiss={onClose}
      header={"Download result images"}
      footer={
        <div className={css`
          display: flex;
          justify-content: end;
        `}>
          <Button variant="primary" onClick={onClose}>
            Done
          </Button>
        </div>

      }
    >
      <SpaceBetween direction="vertical" size="s">
        <TextContent>
          <small>
            This tool allows you to download result images that have been processed by this model and workflow.
          </small>
        </TextContent>
        <Alert type="info">
          The images are ordered by lowest confidence to greatest confidence.
        </Alert>
        <form onSubmit={(e): void => e.preventDefault()}>
          <Form>
            <SpaceBetween direction="vertical" size="l">
              <FormField label="Time range" description="Select the range of time you want images from.">
                <DateRangePicker
                  rangeSelectorMode="absolute-only"
                  onChange={
                    ({ detail: { value } }): void => setTimeRange(value)
                  }
                  value={dateRange}
                  relativeOptions={[]}
                  isValidRange={(range): DateRangePickerProps.ValidationResult => {
                    if (!!range && range.type === "absolute") {
                      const [startDateWithoutTime] = range.startDate.split("T");
                      const [endDateWithoutTime] = range.endDate.split("T");
                      if (!startDateWithoutTime || !endDateWithoutTime) {
                        return {
                          valid: false,
                          errorMessage: "The selected date range is incomplete. Select a start and end date for the date range."
                        };
                      }
                      if (new Date(range.startDate).getTime() - new Date(range.endDate).getTime() > 0) {
                        return {
                          valid: false,
                          errorMessage:
                            "The selected date range is invalid. The start date must be before the end date."
                        };
                      }
                    }
                    return { valid: true };
                  }}
                  i18nStrings={{
                    absoluteModeTitle: "Absolute mode",
                    cancelButtonLabel: "Cancel",
                    clearButtonLabel: "Clear",
                    applyButtonLabel: "Apply",
                  }}
                  placeholder="Choose time range"
                />
              </FormField>
              <FormField label="Image quantity" description="Select the quantity of images">
                <Input
                  type="number"
                  value={imageQuantity}
                  onChange={({ detail }): void => {
                    const numberValue = Number.parseInt(detail.value);
                    if (numberValue < MIN_QUANTITY) {
                      setImageQuantity(`${MIN_QUANTITY}`);
                    } else if (numberValue > MAX_QUANTITY) {
                      setImageQuantity(`${MAX_QUANTITY}`)
                    } else {
                      setImageQuantity(detail.value)
                    }
                  }}
                />
                <TextContent>
                  <small>Numeric values only. Between {MIN_QUANTITY} and {MAX_QUANTITY}.</small>
                </TextContent>
              </FormField>
              <FormField label="Image classification" description="Rule that triggers sending a signal through this output.">
                <Select
                  selectedOption={selectedOption}
                  onChange={({ detail }): void =>
                    setSelectedOption(detail.selectedOption)
                  }
                  options={imageClassificationOptions}
                />
              </FormField>
              <FormField>
                <Button
                  disabled={!isReadyToDownload}
                  variant="normal"
                  iconName="download"
                  loading={isDownloading}
                  onClick={(): void => {
                    if (!dateRange || dateRange.type !== "absolute") return;
                    const endTime = getTimestampInSeconds(new Date(dateRange.endDate).getTime());
                    const startTime = getTimestampInSeconds(new Date(dateRange.startDate).getTime());
                    setIsDownloading(true);
                    retrainInputImages(workflowId, {
                      startTime,
                      endTime,
                      inputImageLimit: Number.parseInt(imageQuantity),
                      predictionResult: selectedOption.value as PredictionResult,
                      ...(authEnabled ? { token } : {})
                    })
                      .then(() => setShowErrorAlert(NaN))
                      .catch((errorStatusCode) => setShowErrorAlert(errorStatusCode))
                      .finally(() => setIsDownloading(false));
                  }}
                >Download result images</Button>
              </FormField>
              {
                !!showErrorAlert && (
                  <Alert type="error">
                    {showErrorAlert === 442 ? "The defined criteria did not return any results." : "An error occurred. Try downloading again."}
                  </Alert>
                )
              }
            </SpaceBetween>
          </Form>

        </form>
      </SpaceBetween>
    </Modal>
  );
}