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
  SpaceBetween,
  StatusIndicator,
} from "@cloudscape-design/components";
import { useMutation } from "@tanstack/react-query";
import { snapshot } from "api/Snapshot";
import { Connection } from "../../config/Interface";
import { ValueWithLabel } from "Common";
import { useEffect, useState } from "react";
import useAuth from "components/auth/authHook";

export default function DownloadLogs(): JSX.Element {
  const [download, setDownload] = useState(false);
  const [errorMessage, setErrorMessage] = useState(false);
  const { token, authEnabled } = useAuth();
  const snapshotMutation = useMutation({
    mutationFn: () => snapshot(),
    onSuccess: () => {
      setErrorMessage(false);
    },
    onError: () => {
      setErrorMessage(true);
    },
  });

  useEffect(() => {
    if (snapshotMutation.data && download) {
      const link = document.createElement("a");
      link.href = Connection.ENDPOINT + "/" + snapshotMutation.data + `${authEnabled ? `?token=${encodeURIComponent(token)}` : ""}`;
      link.click();
      setDownload(false);
    }
  }, [snapshotMutation.data, download, authEnabled, token]);

  return (
    <ValueWithLabel label="Download application logs" labelBoxVariant="h3">
      <Box padding={{ top: "xs" }}>
        <SpaceBetween size="xs">
          <Button
            target="_blank"
            rel="noreferrer"
            onClick={(): void => {
              snapshotMutation.mutate();
              setDownload(true);
              setErrorMessage(false);
            }}
            loading={snapshotMutation.isLoading || download}
          >
            Download full logs
          </Button>
          {errorMessage ? (
            <StatusIndicator type="error">
              An error occurred while preparing the logs. Try again.
            </StatusIndicator>
          ) : null}
        </SpaceBetween>
      </Box>
    </ValueWithLabel>
  );
}
