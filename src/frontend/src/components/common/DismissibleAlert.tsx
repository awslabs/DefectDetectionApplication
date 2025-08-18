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
import { Alert, AlertProps } from "@cloudscape-design/components";
import { useState } from "react";
import WarningAlert from "./WarningAlert";

/**
 * A CloudScape Alert component with simple dismiss functionality setup already
 */
export default function DismissibleAlert({ children, ...props }: Omit<AlertProps, "onDismiss" | "dismissible">): JSX.Element {

  const [dismissed, setDismissed] = useState(false);
  const onDismiss = (): void => setDismissed(true);

  if (dismissed) return <></>;

  if (props.type === "warning") {
    return (
      <WarningAlert dismissible onDismiss={onDismiss} {...props}>
        {children}
      </WarningAlert>
    );
  }

  return (
    <Alert dismissible onDismiss={onDismiss} {...props}>
      {children}
    </Alert>
  );
}