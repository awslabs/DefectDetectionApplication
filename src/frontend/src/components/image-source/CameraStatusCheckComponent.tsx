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

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CameraStatus } from "./types";
import { getImageSource } from "api/ImageSourceAPI";
import { useEffect, useRef, useState } from "react";
import { connectCamera } from "api/CameraAPI";
import ErrorCameraModal from "components/live-result/ErrorCameraModal";

/**
 * @deprecated
 */
export default function CameraStatusCheckComponent({ id, onReconnect }: { id: string; onReconnect?: () => void }): JSX.Element {

    const [cameraStatus, setCameraStatus] = useState("");
    const [showErrorModal, setShowErrorModal] = useState(false);
    const queryClient = useQueryClient();
    const reconnectTriggered = useRef<boolean>(false);

    const { data: imageSource } = useQuery({
        queryKey: ["checkCameraStatus", id],
        queryFn: () => getImageSource(id),
        enabled: !!id,
    })

    const { mutate: reconnect } = useMutation({
        mutationFn: async (cameraId: string) => {
            await connectCamera(cameraId);
            queryClient.refetchQueries(["checkCameraStatus", id]);
        },
        onError: () => {
            setShowErrorModal(true);
        }
    });

    useEffect(() => {
        if (imageSource?.cameraStatus?.status) {
            setCameraStatus(imageSource.cameraStatus.status)
        }
    }, [imageSource?.cameraStatus?.status]);

    /**
     * Auto reconnect once when camera status is disconnected
     */
    useEffect(() => {
        if (cameraStatus === CameraStatus.Disconnected && imageSource?.cameraId && !reconnectTriggered.current) {
            reconnect(imageSource.cameraId);
            reconnectTriggered.current = true;
        }
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [cameraStatus, imageSource?.cameraId])

    return (
        <ErrorCameraModal
            isVisible={showErrorModal}
            onCancel={(): void => setShowErrorModal(false)}
            cameraId={imageSource?.cameraId || ""}
            onRetry={(): void => {
                setShowErrorModal(false);
                queryClient.refetchQueries(["checkCameraStatus", id]);
                reconnect(imageSource?.cameraId || "");
            }}
        />
    );
}