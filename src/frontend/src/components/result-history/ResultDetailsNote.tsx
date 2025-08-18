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

import { Button, ExpandableSection, SpaceBetween, TextContent } from "@cloudscape-design/components";
import { useEffect, useState } from "react";
import NotesEditForm, { DEFAULT_NOTES_EDIT_FORM_ID } from "./NotesEditForm";

interface ResultDetailsNoteProps {
  initialNotes?: string;
  onSave: (notes: string) => void;
  isUpdating: boolean;
  isEditMode: boolean;
  onChangeEditMode: (isEditMode: boolean) => void;
}

export default function ResultDetailsNote({ initialNotes = "", onSave, isUpdating, isEditMode, onChangeEditMode }: ResultDetailsNoteProps): JSX.Element {
  const [notes, setNotes] = useState("");

  useEffect(() => {
    if (!isEditMode) {
      setNotes("");
    } else {
      setNotes(initialNotes);
    }
  }, [initialNotes, isEditMode]);

  return (
    <ExpandableSection
      defaultExpanded
      variant="container"
      headerText="Notes"
      headerActions={
        isEditMode
          ? (
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" disabled={isUpdating} onClick={(): void => onChangeEditMode(false)}>Cancel</Button>
              <Button variant="normal" loading={isUpdating} disabled={isUpdating} form={DEFAULT_NOTES_EDIT_FORM_ID}>Save</Button>
            </SpaceBetween>
          )
          : <Button variant="normal" onClick={(): void => onChangeEditMode(true)}>Edit</Button>
      }
    >
      {
        isEditMode
          ? (
            <NotesEditForm
              notes={notes}
              onChange={setNotes}
              onSave={onSave}
            />
          )
          : (
            <SpaceBetween direction="vertical" size="xxxs">
              <TextContent>
                <b>Note</b>
                <p>{initialNotes}</p>
              </TextContent>
            </SpaceBetween>

          )
      }
    </ExpandableSection>
  );
}