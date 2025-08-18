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
import { Flashbar, TextContent } from "@cloudscape-design/components";
import Modal from "@cloudscape-design/components/modal";
import { css } from "@emotion/css";
import * as awsui from "@cloudscape-design/design-tokens/index";
interface RestartingApplicationModalProps {
  isVisible: boolean;
}

const closeButtonOverlayCss = css`
  position: relative;
  * {
    position: absolute;
    background: ${awsui.colorBackgroundButtonNormalDefault};
    height: 20px;
    width: 20px;
    top: -42px;
    right: 3px;
  }
`;

export default function RestartingApplicationModal(
  props: RestartingApplicationModalProps,
): JSX.Element {
  return (
    <Modal visible={props.isVisible} header={"Restarting application"}>
      <div id="testID" className={closeButtonOverlayCss}>
        <div></div>
      </div>
      <Flashbar
        items={[
          {
            type: "success",
            loading: true,
            content: (
              <TextContent>
                <p style={{ color: "white" }}>
                  <strong>Station application is restarting.</strong>
                </p>
                <p style={{ color: "white" }}>
                  The main station console interface will remain unavailable
                  while the system is restarting. This operation may take
                  between 1-2 minutes. Once complete this page will provide an
                  update on the restart status.
                </p>
              </TextContent>
            ),
            dismissible: false,
          },
        ]}
      />
    </Modal>
  );
}
