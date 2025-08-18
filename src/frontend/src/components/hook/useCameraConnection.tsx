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
import { Button } from "@cloudscape-design/components";
import { useMutation } from "@tanstack/react-query";
import { connectCamera, disconnectCamera } from "api/CameraAPI";
import { CAMERA_CONNECTING_NOTIFICATION_ID, CAMERA_CONNECT_FAILED_NOTIFICATION_ID, CAMERA_DISCONNECT_FAILED_NOTIFICATION_ID } from "components/image-source/constants";
import { AppLayoutContext } from "components/layout/AppLayoutContext";
import { useContext } from "react";

interface UseCameraConnectionHook {
  disconnect: () => void;
  connect: () => void;
  isDisconnecting: boolean;
  isConnecting: boolean;
}

export default function useCameraConnection({
  cameraId,
  recheckStatusFn,
}: {
  cameraId: string;
  recheckStatusFn: () => void;
}): UseCameraConnectionHook {
  const { addSuccess, addError, addNotification, removeNotification } = useContext(AppLayoutContext);

  const connectingNotificationId = `${CAMERA_CONNECTING_NOTIFICATION_ID}-${cameraId}`;
  const connectFailedNotificationId = `${CAMERA_CONNECT_FAILED_NOTIFICATION_ID}-${cameraId}`;
  const disconnectFailedNotificationId = `${CAMERA_DISCONNECT_FAILED_NOTIFICATION_ID}-${cameraId}`;

  const { mutate: disconnect, isLoading: isDisconnecting } = useMutation({
    mutationFn: () => disconnectCamera(cameraId),
    onSuccess: () => recheckStatusFn(),
    onMutate: (() => removeNotification(disconnectFailedNotificationId)),
    onError: () => addError({
      header: `Failed to disconnect from ${cameraId}`,
      content: "An error occurred while attempting to disconnect from the camera. Try again.",
      id: disconnectFailedNotificationId,
      action: <Button onClick={(): void => disconnect()}>Retry</Button>,
    }),
  });

  const { mutate: connect, isLoading: isConnecting } = useMutation({
    mutationFn: () => connectCamera(cameraId),
    onSuccess: () => {
      recheckStatusFn();
      removeNotification(connectingNotificationId);
      addSuccess({
        content: (<>You successfully connected <strong>{cameraId}</strong>.</>),
      });
    },
    onMutate: () => {
      // react batches setState function calls so race condition shouldn't happen here
      removeNotification(connectFailedNotificationId);
      addNotification({
        content: (<>Connecting to <strong>{cameraId}</strong>.</>),
        id: connectingNotificationId,
        loading: true,
      });
    },
    onError: () => {
      removeNotification(connectingNotificationId);
      addError({
        header: `Failed to connect to ${cameraId}`,
        content: "Verify that your camera is powered on and not already connected to another station or device.",
        id: connectFailedNotificationId,
        action: <Button onClick={(): void => connect()}>Retry</Button>,
      });
    },
  });

  return {
    disconnect,
    connect,
    isDisconnecting,
    isConnecting,
  }
}