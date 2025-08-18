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

import { Box, Button, Header, Modal, SpaceBetween } from "@cloudscape-design/components";
import { ReactNode, useEffect, useState } from "react";
import WarningAlert from "components/common/WarningAlert";
import NotesEditForm, { DEFAULT_NOTES_EDIT_FORM_ID } from "./NotesEditForm";

interface EditNotesModalProps {
  showModal: boolean;
  onClose: () => void;
  showHasNoteAlert: boolean;
  initialNotes?: string;
  onSave: (notes: string) => void;
  isSaving: boolean;
  description?: ReactNode;
}

export default function EditNotesModal({
  showModal,
  onClose,
  showHasNoteAlert,
  initialNotes = "",
  onSave,
  isSaving,
  description,
}: EditNotesModalProps): JSX.Element {

  const [notes, setNotes] = useState("");

  useEffect(() => {
    if (!showModal) {
      // reset notes on modal close
      setNotes("");
    } else if (initialNotes) {
      setNotes(initialNotes);
    }
  }, [showModal, initialNotes]);

  return (
    <Modal
      visible={showModal}
      onDismiss={onClose}
      header={
        <Header variant="h1">
          Edit notes
        </Header>
      }
      footer={(
        <Box float="right">
          <SpaceBetween size="xs" direction="horizontal">
            <Button
              variant="link"
              onClick={onClose}
              data-test-id="note-edit-cancel-button"
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={isSaving}
              form={DEFAULT_NOTES_EDIT_FORM_ID}
              data-test-id="note-edit-save-button"
            >
              Save
            </Button>
          </SpaceBetween>
        </Box>
      )}
    >
      <SpaceBetween direction="vertical" size="xs" data-test-id="note-edit-modal-content">
        {description}
        {
          showHasNoteAlert && (
            <WarningAlert data-test-id="note-override-warning-alert">
              Your selection includes results that already have different notes. If you proceed they will all be replaced.
            </WarningAlert>
          )
        }
        <NotesEditForm
          notes={notes}
          onChange={setNotes}
          onSave={(notes): void => onSave(notes)}
        />
      </SpaceBetween>
    </Modal>
  );
}