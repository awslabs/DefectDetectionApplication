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
  SpaceBetween,
  Spinner,
} from "@cloudscape-design/components";
import FormInput from "../../form/FormInput";
import FormTextarea from "../../form/FormTextarea";
import { DESCRIPTION_MAX } from "components/form/constants";

interface WorkflowDetailsProps {
  isLoading: boolean;
}

export default function WorkflowDetails({ isLoading }: WorkflowDetailsProps) {
  return (
    <Container header={<Header variant="h2">Workflow details</Header>}>
      {isLoading ? (
        <Spinner size="big" />
      ) : (
        <SpaceBetween direction="vertical" size="l">
          <FormInput
            stretch
            name="name"
            label="Workflow name"
            description="The name that you enter here can help you distinguish the workflow from others."
            placeholder="Product Line 2 - Dent verification"
            constraintText="Valid characters are a-z, A-Z, 0-9, _ (underscore), spaces, and - (hyphen)."
          />

          <FormTextarea
            stretch
            name="description"
            max={DESCRIPTION_MAX}
            label={
              <>
                Description - <em>optional</em>
              </>
            }
            description="This description can help you quickly identify what the workflow is used for."
            placeholder="This workflow is used for dent detection at location XYZ."
          />
        </SpaceBetween>
      )}
    </Container>
  );
}
