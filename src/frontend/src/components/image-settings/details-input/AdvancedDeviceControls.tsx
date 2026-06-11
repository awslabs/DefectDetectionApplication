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
  FormField,
  Input,
  Select,
  SpaceBetween,
  Toggle,
} from "@cloudscape-design/components";
import { useEffect, useMemo, useRef } from "react";
import { useFormContext, useWatch } from "react-hook-form";
import { CameraFeatureBounds } from "../../../api/CameraAPI";
import {
  AdvancedFeature,
  advFieldName,
  getSupportedAdvancedFeatures,
} from "../advancedFeatures";

interface AdvancedDeviceControlsProps {
  bounds?: CameraFeatureBounds;
}

/**
 * Advanced (Tier 2 / Tier 3) GenICam controls a camera supports. The controls
 * are bound to the same form as gain/exposure, so their values flow through the
 * preview/capture config and are applied to the device on the acquisition path
 * (the preview updates live as they change). Initialized from what the device
 * reports on load. Renders nothing when the camera reports no advanced
 * controls.
 */
export default function AdvancedDeviceControls({
  bounds,
}: AdvancedDeviceControlsProps) {
  const { setValue, control } = useFormContext();
  const features = useMemo(
    () => getSupportedAdvancedFeatures(bounds),
    [bounds],
  );

  // Seed the form fields from the device-reported current values once per
  // bounds load, so the controls reflect "what the camera is reporting".
  const seededRef = useRef<string>("");
  useEffect(() => {
    const sig = features.map((f) => f.key).join(",");
    if (!features.length || seededRef.current === sig) return;
    for (const f of features) {
      if (f.bound.current != null) {
        setValue(advFieldName(f.key), f.bound.current as any);
      }
    }
    seededRef.current = sig;
  }, [features, setValue]);

  if (features.length === 0) return null;

  return (
    <SpaceBetween size="m">
      <Box variant="h4">Camera controls</Box>
      {features.map((f) => (
        <FormField key={f.key} label={f.label} description={f.description}>
          <AdvancedControl feature={f} control={control} setValue={setValue} />
        </FormField>
      ))}
    </SpaceBetween>
  );
}

function AdvancedControl({
  feature,
  control,
  setValue,
}: {
  feature: AdvancedFeature;
  control: any;
  setValue: (name: string, value: any) => void;
}): JSX.Element {
  const name = advFieldName(feature.key);
  const value = useWatch({ control, name });
  const { bound } = feature;

  if (bound.type === "boolean") {
    return (
      <Toggle
        checked={Boolean(value)}
        onChange={({ detail }) => setValue(name, detail.checked)}
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
        onChange={({ detail }) => setValue(name, detail.selectedOption.value)}
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
        if (!Number.isNaN(parsed)) setValue(name, parsed);
      }}
    />
  );
}
