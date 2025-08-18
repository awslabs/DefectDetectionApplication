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
  Container,
  Header,
  Popover,
  StatusIndicator,
  Button,
} from "@cloudscape-design/components";
import { ValueWithLabel } from "Common";

interface OutputPathProps {
  workflowOutputPath: string;
  onClickDownload: () => void;
}

export default function OutputPathContainer(
  {
    workflowOutputPath,
    onClickDownload
  }: OutputPathProps,
): JSX.Element {
  return (
    <Container
      header={
        <Header
          variant="h2"
          description="The path where all the outputs will be stored."
          actions={
            <Button
              variant="normal"
              onClick={onClickDownload}
            >
              Download result images
            </Button>
          }
        >
          Output path
        </Header>
      }
    >
      <ValueWithLabel label="URL">
        <Popover
          size="small"
          position="top"
          dismissButton={false}
          triggerType="custom"
          content={<StatusIndicator type="success">URL copied</StatusIndicator>}
        >
          <Button
            variant="inline-icon"
            iconName="copy"
            ariaLabel="Copy URL"
            onClick={(): Promise<void> =>
              navigator.clipboard.writeText(workflowOutputPath)
            }
          />
          {workflowOutputPath}
        </Popover>
      </ValueWithLabel>
    </Container>
  );
}
