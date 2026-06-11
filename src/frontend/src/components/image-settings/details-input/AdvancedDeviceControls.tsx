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
  Box,
  Button,
  FormField,
  Input,
  Select,
  SpaceBetween,
  Toggle,
} from "@cloudscape-design/components";
import { useContext, useEffect, useMemo, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import {
  ApplyCameraFeature,
  CameraFeatureBounds,
  applyCameraFeatures,
} from "../../../api/CameraAPI";
import {
  AdvancedFeature,
  getSupportedAdvancedFeatures,
} from "../advancedFeatures";
import { AppLayoutContext } from "../../layout/AppLayoutContext";

type FeatureValue = string | number | boolean;

interface AdvancedDeviceControlsProps {
  cameraId: string;
  bounds?: CameraFeatureBounds;
}

function initialValues(features: AdvancedFeature[]): Record<string, FeatureValue> {
  const values: Record<string, FeatureValue> = {};
  for (const f of features) {
    if (f.bound.current != null) {
      values[f.key] = f.bound.current as FeatureValue;
    }
  }
  return values;
}

/**
 * Renders the advanced (Tier 2 / Tier 3) GenICam controls a camera supports.
 * Values are initialized from what the device reports when the page loads, and
 * changes are applied to the live camera. Renders nothing when the camera
 * reports no advanced controls.
 */
export default function AdvancedDeviceControls({
  cameraId,
  bounds,
}: AdvancedDeviceControlsProps) {
  const { addSuccess, addError } = useContext(AppLayoutContext);
  const features = useMemo(
    () => getSupportedAdvancedFeatures(bounds),
    [bounds],
  );
  const [values, setValues] = useState<Record<string, FeatureValue>>(() =>
    initialValues(features),
  );

  // Re-seed from the device whenever the reported bounds change (initial load
  // or refetch), so controls always reflect "what the camera is reporting".
  useEffect(() => {
    setValues(initialValues(features));
  }, [features]);

  const applyMutation = useMutation({
    mutationFn: () => {
      const changed: ApplyCameraFeature[] = features
        .filter((f) => values[f.key] !== undefined && values[f.key] !== f.bound.current)
        .map((f) => ({
          feature: f.bound.feature ?? f.key,
          type: f.bound.type,
          value: values[f.key],
        }));
      return applyCameraFeatures(cameraId, changed);
    },
    onSuccess: (applied) => {
      // Reflect the device-accepted values (it may clamp/coerce).
      setValues((prev) => {
        const next = { ...prev };
        for (const f of features) {
          const fname = f.bound.feature ?? f.key;
          if (applied[fname] !== undefined) next[f.key] = applied[fname];
        }
        return next;
      });
      addSuccess({ content: <>Camera settings applied.</> });
    },
    onError: (error: any) => {
      addError({
        content: (
          <>
            Failed to apply camera settings.{" "}
            {error?.response?.data?.message ?? error?.message ?? ""}
          </>
        ),
      });
    },
  });

  if (features.length === 0) return null;

  const setValue = (key: string, value: FeatureValue): void =>
    setValues((prev) => ({ ...prev, [key]: value }));

  return (
    <SpaceBetween size="m">
      <Box variant="h4">Camera controls</Box>
      {features.map((f) => (
        <FormField key={f.key} label={f.label} description={f.description}>
          {renderControl(f, values[f.key], (v) => setValue(f.key, v))}
        </FormField>
      ))}
      <Button
        formAction="none"
        variant="normal"
        loading={applyMutation.isLoading}
        onClick={() => applyMutation.mutate()}
      >
        Apply camera settings
      </Button>
    </SpaceBetween>
  );
}

function renderControl(
  feature: AdvancedFeature,
  value: FeatureValue | undefined,
  onChange: (value: FeatureValue) => void,
): JSX.Element {
  const { bound } = feature;

  if (bound.type === "boolean") {
    return (
      <Toggle
        checked={Boolean(value)}
        onChange={({ detail }) => onChange(detail.checked)}
      />
    );
  }

  if (bound.type === "enumeration") {
    const options = (bound.options ?? []).map((o) => ({ label: o, value: o }));
    const selectedOption =
      value != null ? { label: String(value), value: String(value) } : null;
    return (
      <Select
        selectedOption={selectedOption}
        options={options}
        onChange={({ detail }) =>
          onChange(detail.selectedOption.value as string)
        }
      />
    );
  }

  // integer / float
  return (
    <Input
      type="number"
      value={value != null ? `${value}` : ""}
      onChange={({ detail }) => {
        const parsed = Number(detail.value);
        if (!Number.isNaN(parsed)) onChange(parsed);
      }}
    />
  );
}
