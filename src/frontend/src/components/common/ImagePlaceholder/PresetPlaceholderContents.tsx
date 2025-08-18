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
import { Button, SpaceBetween } from "@cloudscape-design/components";

export function CameraDisconnectedContent({ loading, onConnect, message }: { loading: boolean; onConnect: () => void; message?: string }): JSX.Element {
  return (
    <SpaceBetween direction="vertical" size="m" alignItems="center">
      <span>
        {message ?? "Camera disconnected, unable to load preview."}
      </span>
      {/* Added formAction none here to avoid form submit action auto-bind */}
      <Button formAction="none" variant="normal" loading={loading} onClick={onConnect}>
        Connect to camera
      </Button>
    </SpaceBetween>
  );
}
