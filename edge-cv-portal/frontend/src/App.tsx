import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import '@cloudscape-design/global-styles/index.css';
import { AuthProvider } from './contexts/AuthContext';
import Dashboard from './pages/Dashboard';
import UseCases from './pages/UseCases';
import UseCaseOnboarding from './pages/UseCaseOnboarding';
import Devices from './pages/Devices';
import DeviceDetail from './pages/DeviceDetail';
import Models from './pages/Models';
import ModelDetail from './pages/ModelDetail';
import Training from './pages/Training';
import TrainingDetail from './pages/TrainingDetail';
import CreateTraining from './pages/CreateTraining';
import ImportModel from './pages/ImportModel';
import SmartImport from './pages/SmartImport';
import Labeling from './pages/Labeling';
import LabelingDetail from './pages/LabelingDetail';
import CreateLabelingJob from './pages/CreateLabelingJob';
import DatasetBrowser from './pages/DatasetBrowser';
import PreLabeledDatasets from './pages/PreLabeledDatasets';
import TransformManifest from './pages/TransformManifest';
import DataManagement from './pages/DataManagement';
import Deployments from './pages/Deployments';
import DeploymentDetail from './pages/DeploymentDetail';
import CreateDeployment from './pages/CreateDeployment';
import Components from './pages/Components';
import ComponentDetail from './pages/ComponentDetail';
import ComponentConfiguration from './pages/ComponentConfiguration';
import Settings from './pages/Settings';
import AuditLogs from './pages/AuditLogs';
import Login from './pages/Login';
import Layout from './components/Layout';
import ProtectedRoute from './components/ProtectedRoute';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      refetchOnWindowFocus: false,
      retry: 1,
      staleTime: 5 * 60 * 1000, // 5 minutes
    },
  },
});

function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AuthProvider>
        <Router>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route
              path="/"
              element={
                <ProtectedRoute>
                  <Layout />
                </ProtectedRoute>
              }
            >
              <Route index element={<Navigate to="/dashboard" replace />} />
              <Route path="dashboard" element={<Dashboard />} />
              <Route path="usecases" element={<UseCases />} />
              <Route path="usecases/onboard" element={<UseCaseOnboarding />} />
              <Route path="labeling" element={<Labeling />} />
              <Route path="labeling/datasets" element={<DatasetBrowser />} />
              <Route path="data" element={<DataManagement />} />
              <Route path="labeling/pre-labeled" element={<PreLabeledDatasets />} />
              <Route path="labeling/transform-manifest" element={<TransformManifest />} />
              <Route path="labeling/create" element={<CreateLabelingJob />} />
              <Route path="labeling/:jobId" element={<LabelingDetail />} />
              <Route path="devices" element={<Devices />} />
              <Route path="devices/:deviceId" element={<DeviceDetail />} />
              <Route path="models" element={<Models />} />
              <Route path="models/import" element={<ImportModel />} />
              <Route path="models/smart-import" element={<SmartImport />} />
              <Route path="models/:modelId" element={<ModelDetail />} />
              <Route path="training" element={<Training />} />
              <Route path="training/create" element={<CreateTraining />} />
              <Route path="training/:trainingId" element={<TrainingDetail />} />
              <Route path="deployments" element={<Deployments />} />
              <Route path="deployments/create" element={<CreateDeployment />} />
              <Route path="deployments/:deploymentId" element={<DeploymentDetail />} />
              <Route path="components" element={<Components />} />
              <Route path="components/:arn" element={<ComponentDetail />} />
              <Route path="components/configure" element={<ComponentConfiguration />} />
              <Route path="settings" element={<Settings />} />
              <Route path="audit" element={<AuditLogs />} />
            </Route>
          </Routes>
        </Router>
      </AuthProvider>
    </QueryClientProvider>
  );
}

export default App;
