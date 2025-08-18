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

interface DetailsInputProps {
  namePrefix: string;
  isLoading: boolean;
}

/**
 * Used across create and edit views.
 *
 * UX wants the image source name to be auto-populated if Camera type is
 * selected. We should think about how to properly handle this when you switch
 * between Camera and Folder types. For example, when you switch from Camera to
 * Folder type, we should clear the name, but if you switch back to Camera, your
 * previous camera name should be repopulated. My solution to this is to have
 * separate image source details for each type.
 */
export default function DetailsInput({
  namePrefix,
  isLoading,
}: DetailsInputProps): JSX.Element {
  return (
    <Container header={<Header variant="h2">Image source details</Header>}>
      {isLoading ? (
        <Spinner size="big" />
      ) : (
        <SpaceBetween direction="vertical" size="l">
          <FormInput
            name={namePrefix + "Name"}
            stretch
            label="Image source name"
            description="The name that you enter here appears in the results view.  It can help you distinguish the image source from others."
            placeholder="Product Line 2 - Dent verification"
            constraintText="Valid characters are a-z, A-Z, 0-9, _ (underscore), spaces, and - (hyphen)."
          />

          <FormTextarea
            name={namePrefix + "Description"}
            stretch
            max={DESCRIPTION_MAX}
            label={
              <>
                Description - <em>optional</em>
              </>
            }
            description="This description can help you quickly identify what the image source is used for."
            placeholder="This image source is used for dent detection at location XYZ."
          />
        </SpaceBetween>
      )}
    </Container>
  );
}
