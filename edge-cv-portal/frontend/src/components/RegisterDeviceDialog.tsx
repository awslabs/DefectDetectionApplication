import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Autosuggest,
  Box,
  Button,
  Form,
  FormField,
  Input,
  Modal,
  SpaceBetween,
} from '@cloudscape-design/components';
import type { AutosuggestProps } from '@cloudscape-design/components';
import {
  ApiError,
  apiService,
  RegistrationWithCommand,
} from '../services/api';
import { getErrorMessage } from '../utils/errorHandling';

/**
 * IoT Thing / Thing Group name pattern (station-quick-setup Requirement 1.2):
 * 1–128 characters drawn from letters, digits, colon (`:`), underscore (`_`),
 * and hyphen (`-`). A new Device_Group name must satisfy the same pattern
 * (Requirement 1.8), so the field validation is shared.
 */
export const IOT_NAME_PATTERN = /^[a-zA-Z0-9:_-]{1,128}$/;

const PATTERN_HINT =
  'Use 1–128 characters: letters, digits, colon (:), underscore (_), or hyphen (-).';

export interface RegisterDeviceDialogProps {
  /** Whether the dialog is shown. */
  visible: boolean;
  /**
   * The Use_Case the registration is created under, taken from the current
   * portal context (Requirement 1.1). When absent the form cannot be
   * submitted.
   */
  usecaseId: string | null;
  /** Dismiss the dialog without registering. */
  onDismiss: () => void;
  /**
   * Called with the created Device_Registration and its one-line
   * Setup_Command once registration succeeds, so the caller can present the
   * `SetupCommandDialog` (task 9.3).
   */
  onRegistered: (result: RegistrationWithCommand) => void;
}

/**
 * Validate an IoT Thing / Thing Group name for a single field, returning a
 * per-field message (Requirements 1.2, 1.9) or `null` when the value is
 * acceptable.
 */
function validateName(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return 'This field is required.';
  }
  if (!IOT_NAME_PATTERN.test(trimmed)) {
    return PATTERN_HINT;
  }
  return null;
}

/**
 * `RegisterDeviceDialog` — register a new edge device from the portal
 * (station-quick-setup Requirements 1.2, 1.7, 1.8).
 *
 * The device-name field validates against the IoT name pattern with per-field
 * feedback, and the Device_Group field is an autocomplete populated from the
 * Use_Case's existing IoT Thing Groups (`listThingGroups`, Requirement 1.7)
 * that also accepts a free-text new group name (Requirement 1.8). The Use_Case
 * is taken from the current context via the `usecaseId` prop.
 */
export default function RegisterDeviceDialog({
  visible,
  usecaseId,
  onDismiss,
  onRegistered,
}: RegisterDeviceDialogProps) {
  const [deviceName, setDeviceName] = useState('');
  const [deviceGroup, setDeviceGroup] = useState('');

  // Per-field errors: null = valid/untouched, string = message to display.
  const [deviceNameError, setDeviceNameError] = useState<string | null>(null);
  const [deviceGroupError, setDeviceGroupError] = useState<string | null>(null);

  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  // Existing IoT Thing Groups for the autocomplete (Requirement 1.7).
  const [thingGroups, setThingGroups] = useState<string[]>([]);
  const [groupsLoading, setGroupsLoading] = useState(false);
  const [groupsError, setGroupsError] = useState<string | null>(null);

  // Reset all form state whenever the dialog is (re)opened.
  useEffect(() => {
    if (visible) {
      setDeviceName('');
      setDeviceGroup('');
      setDeviceNameError(null);
      setDeviceGroupError(null);
      setFormError(null);
      setGroupsError(null);
    }
  }, [visible]);

  // Load the Use_Case's existing Thing Groups when the dialog opens.
  useEffect(() => {
    if (!visible || !usecaseId) {
      setThingGroups([]);
      return;
    }

    let cancelled = false;
    const loadGroups = async () => {
      try {
        setGroupsLoading(true);
        setGroupsError(null);
        const response = await apiService.listThingGroups(usecaseId);
        if (!cancelled) {
          setThingGroups(response.thing_groups || []);
        }
      } catch (err) {
        if (!cancelled) {
          // Non-fatal: the operator can still type a new group name.
          setThingGroups([]);
          setGroupsError(
            getErrorMessage(err, 'Could not load existing device groups.')
          );
        }
      } finally {
        if (!cancelled) {
          setGroupsLoading(false);
        }
      }
    };

    loadGroups();
    return () => {
      cancelled = true;
    };
  }, [visible, usecaseId]);

  const groupOptions: AutosuggestProps.Options = useMemo(
    () => thingGroups.map((name) => ({ value: name })),
    [thingGroups]
  );

  const handleDeviceNameChange = useCallback((value: string) => {
    setDeviceName(value);
    // Re-validate on every keystroke for live per-field feedback.
    setDeviceNameError(validateName(value));
    setFormError(null);
  }, []);

  const handleDeviceGroupChange = useCallback((value: string) => {
    setDeviceGroup(value);
    setDeviceGroupError(validateName(value));
    setFormError(null);
  }, []);

  const handleSubmit = useCallback(async () => {
    if (submitting) {
      return;
    }

    // Validate all fields up front so every offending field is identified
    // (Requirements 1.2, 1.9).
    const nameError = validateName(deviceName);
    const groupError = validateName(deviceGroup);
    setDeviceNameError(nameError);
    setDeviceGroupError(groupError);

    if (nameError || groupError) {
      return;
    }

    if (!usecaseId) {
      setFormError('Select a use case before registering a device.');
      return;
    }

    try {
      setSubmitting(true);
      setFormError(null);
      const result = await apiService.registerDevice({
        device_name: deviceName.trim(),
        device_group: deviceGroup.trim(),
        usecase_id: usecaseId,
      });
      onRegistered(result);
    } catch (err) {
      const message = getErrorMessage(err, 'Failed to register the device.');
      // A conflicting device name (409) or a device-name validation error is
      // surfaced on the device-name field; anything else is a form-level error.
      const status = err instanceof ApiError ? err.status : undefined;
      if (status === 409 || /device[\s_-]?name/i.test(message)) {
        setDeviceNameError(message);
      } else {
        setFormError(message);
      }
    } finally {
      setSubmitting(false);
    }
  }, [submitting, deviceName, deviceGroup, usecaseId, onRegistered]);

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Register Edge Device"
      size="medium"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss} disabled={submitting}>
              Cancel
            </Button>
            <Button
              variant="primary"
              onClick={handleSubmit}
              loading={submitting}
              disabled={!usecaseId}
            >
              Register device
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <Form>
        <SpaceBetween size="l">
          {formError && (
            <Alert type="error" dismissible onDismiss={() => setFormError(null)}>
              {formError}
            </Alert>
          )}

          {!usecaseId && (
            <Alert type="warning">
              Select a use case before registering a device.
            </Alert>
          )}

          <FormField
            label="Device name"
            description="The IoT Thing name for the new station."
            errorText={deviceNameError ?? undefined}
            constraintText={PATTERN_HINT}
          >
            <Input
              value={deviceName}
              onChange={({ detail }) => handleDeviceNameChange(detail.value)}
              placeholder="station-42"
              ariaLabel="Device name"
              disabled={submitting}
              invalid={!!deviceNameError}
            />
          </FormField>

          <FormField
            label="Device group"
            description="Select an existing IoT Thing Group or type a new group name to create."
            errorText={deviceGroupError ?? undefined}
            constraintText={PATTERN_HINT}
            secondaryControl={
              groupsError ? (
                <Box color="text-status-warning" fontSize="body-s">
                  {groupsError}
                </Box>
              ) : undefined
            }
          >
            <Autosuggest
              value={deviceGroup}
              onChange={({ detail }) => handleDeviceGroupChange(detail.value)}
              options={groupOptions}
              enteredTextLabel={(value) => `Create new group: "${value}"`}
              placeholder="Line3_Group"
              ariaLabel="Device group"
              disabled={submitting}
              invalid={!!deviceGroupError}
              statusType={groupsLoading ? 'loading' : 'finished'}
              loadingText="Loading device groups"
              empty="No existing device groups. Type a name to create one."
              filteringType="auto"
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </Modal>
  );
}
