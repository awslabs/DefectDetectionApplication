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

import { Box, StatusIndicator } from "@cloudscape-design/components";
import { errorMessageDetailStyle } from "components/live-result/styles";

const DEFAULT_ERROR_MESSAGE = "Unable to load the image preview. Review the image settings and try again.";

interface ImagePreviewErrorProps {
  errorMsg?: string;
}

export default function ImagePreviewError({ errorMsg }: ImagePreviewErrorProps): JSX.Element {
  return (
    <StatusIndicator type="error">
      <Box variant="span" color="inherit">
        <span>{DEFAULT_ERROR_MESSAGE}</span>
        {
          !!errorMsg && (
            <>
              <br />
              <span className={errorMessageDetailStyle}>
                {errorMsg}
              </span>
            </>
          )
        }
      </Box>
    </StatusIndicator>
  );
}