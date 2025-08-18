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

import { FormField, Textarea } from "@cloudscape-design/components";
import { Form } from "react-router-dom";
const MAX_CHAR = 50;

export const DEFAULT_NOTES_EDIT_FORM_ID = "notes-edit-id";

interface NotesEditFormProps {
  notes: string;
  onChange: (notes: string) => void;
  onSave: (notes: string) => void;
  formId?: string;
}

export default function NotesEditForm({ notes = "", onChange, onSave, formId = DEFAULT_NOTES_EDIT_FORM_ID }: NotesEditFormProps): JSX.Element {

  return (
    <form
      id={formId}
      onSubmit={(e): void => {
        e.preventDefault();
        if (notes.length <= MAX_CHAR) {
          onSave(notes);
        }
      }}
    >
      <Form>
        <FormField
          errorText={notes.length > MAX_CHAR && `Character limit reached. Notes must be ${MAX_CHAR} characters or less.`}
          constraintText={`${notes.length > MAX_CHAR ? 0 : MAX_CHAR - notes.length}/${MAX_CHAR} characters remaining.`}
          label={<span>Notes</span>}
        >
          <Textarea
            data-test-id="note-edit-form-textarea"
            value={notes}
            name="notes"
            onChange={({ detail: { value } }): void => onChange(value)}
            placeholder="Type notes here"
          />
        </FormField>
      </Form>
    </form>
  );
}