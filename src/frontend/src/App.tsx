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
  Navigate,
  Route,
  RouterProvider,
  createBrowserRouter,
  createRoutesFromElements,
} from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import Layout from "components/layout/Layout";
import AddImageSource from "components/image-source/add/AddImageSource";
import ImageSourceDetails from "components/image-source/details/ImageSourceDetails";
import EditImageSource from "components/image-source/edit/EditImageSource";
import LiveResults from "components/live-result/LiveResults";
import DeployedModels from "components/model/DeployedModels";
import EditWorkflow from "components/workflow/edit/EditWorkflow";
import ListWorkflows from "components/workflow/list/ListWorkflows";
import WorkflowDetails from "components/workflow/details/WorkflowDetails";
import { AppLayoutProvider } from "components/layout/AppLayoutContext";
import EditImageSettings from "./components/image-settings/EditImageSettings";
import { createQueryClient } from "components/react-query";
import ApplicationHealthOverview from "components/application-health/ApplicationHealthOverview";
import EditRoI from "components/image-source/roi/EditRoI";
import ResultHistory from "components/result-history/ResultHistory";
import ResultDetails from "components/result-history/ResultDetails";
import { DynamicRouterHashKey } from "components/layout/constants";
import AuthProvider from "components/auth/AuthProvider";
import "components/auth/AuthContextProvider";
import { AuthContextProvider } from "components/auth/AuthContextProvider";
import ListImageSources from "components/image-source/list/ListImageSources";
import ImageCapturePage from "components/image-source/image-capture/ImageCapturePage";
import ImageCaptureResultHistory from "components/result-history/ImageCaptureResultHistory";
import { HistoryResultPageType } from "components/result-history/types";

const queryClient = createQueryClient();

const router = createBrowserRouter(
  createRoutesFromElements(
    <Route
      path="/"
      // Wrapping context here because AppLayoutProvider needs access to React
      // Router context but Layout needs access to AppLayoutContext
      element={
        <AuthContextProvider>
          <AppLayoutProvider>
            <AuthProvider>
              <Layout />
            </AuthProvider>
          </AppLayoutProvider>
        </AuthContextProvider>
      }
      // TODO: Better error element for general errors
      errorElement={<></>}
    >
      <Route
        path="/"
        // TODO: UX would like if there are no workflows configured to redirect to image sources
        // https://www.figma.com/file/hKBVAWa8TIaSBQlsTFTRDy?node-id=3643:223595#440070758
        element={<Navigate replace to="/result" />}
      />

      <Route path="image-sources" handle={{ breadcrumb: "Image sources" }}>
        <Route index element={<ListImageSources />} />
        <Route
          path="add"
          element={<AddImageSource />}
          handle={{ breadcrumb: "Add image source" }}
        />
        <Route
          path=":imageSourceId"
          /**
           * Set breadcrumb in format of #{DynamicRouterHashKey}
           * This will dynamically set breadcrumb value to the hash value of {DynamicRouterHashKey} from URL
           * Make sure to use the DynamicRouterHashKey enum for the hash key
           * 
           * e.g. set { breadcrumb: #folderName }, then the breadcrumb will be "abc" if url contains hash params "folderName=abc"
           */
          handle={{ breadcrumb: `#${DynamicRouterHashKey.IMAGE_SOURCE_NAME}` }}
        >
          <Route index element={<ImageSourceDetails />} />
          <Route
            path="edit"
            element={<EditImageSource />}
            handle={{ breadcrumb: "Edit image source" }}
          />
          <Route
            path="edit-settings"
            element={<EditImageSettings />}
            handle={{ breadcrumb: "Edit image settings" }}
          />
          <Route
            path="edit-region-of-interest"
            element={<EditRoI />}
            handle={{ breadcrumb: "Edit region of interest" }}
          />
        </Route>
      </Route>

      <Route path="workflows" handle={{ breadcrumb: "Workflows" }}>
        <Route index element={<ListWorkflows />} />

        <Route
          path=":workflowId"
          handle={{ breadcrumb: `#${DynamicRouterHashKey.WORKFLOW_NAME}` }}
        >
          <Route index element={<WorkflowDetails />} />
          <Route
            path="edit"
            element={<EditWorkflow />}
            handle={{ breadcrumb: "Edit workflow" }}
          />
        </Route>
      </Route>

      <Route
        path="models"
        element={<DeployedModels />}
        handle={{ breadcrumb: "Deployed models" }}
      />

      <Route
        path="result"
        element={<LiveResults />}
        handle={{ breadcrumb: "Run inference" }}
      />

      <Route path="history" handle={{ breadcrumb: "Inference results" }}>
        <Route index element={<ResultHistory />} />
        <Route path=":workflowId" handle={{ breadcrumb: "Workflow" }}>
          <Route index element={<ResultHistory />} />
          <Route
            path="detail/:captureId"
            handle={{ breadcrumb: "Result details" }}
          >
            <Route index element={<ResultDetails pageType={HistoryResultPageType.INFERENCE_RESULT} />} />
          </Route>
        </Route>
      </Route>

      <Route
        path="capture"
        element={<ImageCapturePage />}
        handle={{ breadcrumb: "Capture images" }}
      >
        <Route
          path=":workflowId"
          handle={{ breadcrumb: "Capture images" }}
        >
          <Route index element={<ImageCapturePage />} />
        </Route>
      </Route>

      <Route path="capture-results" handle={{ breadcrumb: "Image capture results" }}>
        <Route index element={<ImageCaptureResultHistory />} />
        <Route path=":workflowId" handle={{ breadcrumb: "Workflow" }}>
          <Route index element={<ImageCaptureResultHistory />} />
          <Route
            path="detail/:captureId"
            handle={{ breadcrumb: "Result details" }}
          >
            <Route index element={<ResultDetails pageType={HistoryResultPageType.CAPTURE_RESULT} />} />
          </Route>
        </Route>
      </Route>

      <Route
        path="application-health"
        element={<ApplicationHealthOverview />}
        handle={{ breadcrumb: "Application health overview" }}
      />
    </Route>
  )
);

export default function App(): JSX.Element {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
