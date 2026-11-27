/**
 * Auto-Labeling settings section (Settings → Auto-Labeling).
 *
 * Lets a PortalAdmin set Images per worker run: how many images of its batch
 * a run of the auto-label worker labels before it puts the rest back on the
 * queue for another run. A blank field means nothing is saved, and the worker
 * uses the default, which is every image of its batch. Users without the
 * PortalAdmin role see an access notice instead of the form, and nothing is
 * loaded. After a save the field shows the value the save returns.
 */
import { useEffect, useState } from 'react';
import { Alert, Box, Button, Form, FormField, Header, Input, SpaceBetween, Spinner } from '@cloudscape-design/components';
import { apiService } from '../services/api';
import { useAuth } from '../contexts/AuthContext';
import { getErrorMessage } from '../utils/errorHandling';

interface ImagesPerRunBounds {
  min: number;
  max: number;
  default: number;
}

// The backend's bounds and default (autolabel_settings.py), used until the
// configuration has loaded and when a response lacks them.
const FALLBACK_BOUNDS: ImagesPerRunBounds = { min: 1, max: 5, default: 5 };

function fieldValue(imagesPerRun: number | null | undefined): string {
  return imagesPerRun == null ? '' : String(imagesPerRun);
}

function numberOr(value: unknown, fallback: number): number {
  return typeof value === 'number' ? value : fallback;
}

// Blank is valid: it saves null, so the worker uses the default.
function validate(value: string, bounds: ImagesPerRunBounds): string | undefined {
  if (value.trim() === '') return undefined;
  const imagesPerRun = Number(value);
  if (Number.isInteger(imagesPerRun) && imagesPerRun >= bounds.min && imagesPerRun <= bounds.max) {
    return undefined;
  }
  return `Images per worker run must be a whole number from ${bounds.min} to ${bounds.max}, or blank for the default`;
}

export default function AutoLabelingSettings() {
  const { user } = useAuth();
  const isPortalAdmin = user?.role === 'PortalAdmin';

  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [fieldError, setFieldError] = useState<string | undefined>(undefined);
  const [imagesPerRun, setImagesPerRun] = useState('');
  const [bounds, setBounds] = useState<ImagesPerRunBounds>(FALLBACK_BOUNDS);

  useEffect(() => {
    if (!isPortalAdmin) return;
    let cancelled = false;
    setLoading(true);
    apiService
      .getAutoLabelingConfiguration()
      .then((response) => {
        if (cancelled) return;
        setBounds({
          min: numberOr(response.min_images_per_run, FALLBACK_BOUNDS.min),
          max: numberOr(response.max_images_per_run, FALLBACK_BOUNDS.max),
          default: numberOr(response.default_images_per_run, FALLBACK_BOUNDS.default),
        });
        setImagesPerRun(fieldValue(response.images_per_run));
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(getErrorMessage(err, 'Failed to load auto-labeling configuration'));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [isPortalAdmin]);

  if (!isPortalAdmin) {
    return (
      <Alert type="info" header="Portal Admin access required">
        Auto-labeling settings can only be viewed and changed by Portal Admins.
      </Alert>
    );
  }

  const handleSave = async () => {
    setError('');
    setSuccess('');
    const validationError = validate(imagesPerRun, bounds);
    setFieldError(validationError);
    if (validationError) return;

    setSaving(true);
    try {
      const response = await apiService.updateAutoLabelingConfiguration({
        images_per_run: imagesPerRun.trim() === '' ? null : Number(imagesPerRun),
      });
      setImagesPerRun(fieldValue(response.images_per_run));
      setSuccess('Auto-labeling configuration saved');
    } catch (err: unknown) {
      // The typed value stays in the field so the admin can retry.
      setError(getErrorMessage(err, 'Failed to save auto-labeling configuration'));
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <Box textAlign="center" padding="l">
        <Spinner /> Loading auto-labeling configuration...
      </Box>
    );
  }

  return (
    <SpaceBetween size="l">
      {error && (
        <Alert type="error" dismissible onDismiss={() => setError('')}>
          {error}
        </Alert>
      )}
      {success && (
        <Alert type="success" dismissible onDismiss={() => setSuccess('')}>
          {success}
        </Alert>
      )}

      <Form
        header={
          <Header
            variant="h2"
            description="How the auto-label worker spreads a labeling job's images over its runs. Only Portal Admins can change these settings."
          >
            Auto-Labeling
          </Header>
        }
        actions={
          <Button variant="primary" onClick={handleSave} loading={saving}>
            Save Configuration
          </Button>
        }
      >
        <SpaceBetween size="m">
          <FormField
            label="Images per worker run"
            constraintText={`From ${bounds.min} to ${bounds.max}. The worker labels this many images per run and puts the rest of its batch back on the queue for another run. Lower it for Grounded-SAM jobs with many labels. Leave blank to use the default, ${bounds.default}.`}
            errorText={fieldError}
          >
            <Input
              type="number"
              value={imagesPerRun}
              onChange={({ detail }) => setImagesPerRun(detail.value)}
              ariaLabel="Images per worker run"
              placeholder={String(bounds.default)}
            />
          </FormField>
        </SpaceBetween>
      </Form>
    </SpaceBetween>
  );
}
