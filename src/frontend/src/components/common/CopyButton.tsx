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
import { Button, Popover, StatusIndicator } from "@cloudscape-design/components";
import { css } from "@emotion/css";

const copyButtonStyle = css`padding-left: 0px !important`;

interface CopyButtonProps {
  onCopy: () => void;
  content: JSX.Element;
}

export default function CopyButton({
  onCopy,
  content,
}: CopyButtonProps): JSX.Element {
  return (
    <>
      <Popover
        size="small"
        position="top"
        triggerType="custom"
        dismissButton={false}
        content={<StatusIndicator type="success">Copied</StatusIndicator>}
      >
        <Button
          variant="inline-link"
          onClick={onCopy}
          className={copyButtonStyle}
          iconName="copy"
        />
      </Popover>
      {content}
    </>
  );
}