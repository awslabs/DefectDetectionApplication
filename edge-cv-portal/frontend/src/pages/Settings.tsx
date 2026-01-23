import { useState, useEffect } from 'react';
import {
  Container,
  Header,
  Tabs,
  Table,
  Button,
  Modal,
  Form,
  FormField,
  Input,
  Textarea,
  SpaceBetween,
  Alert,
  Badge,
  Box,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface DataAccount {
  data_account_id: string;
  name: string;
  description?: string;
  role_arn: string;
  external_id: string;
  region: string;
  status: string;
  created_at: number;
  created_by: string;
  connection_test?: {
    status: string;
    message: string;
  };
}

export default function Settings() {
  const [activeTab, setActiveTab] = useState('data-accounts');
  const [dataAccounts, setDataAccounts] = useState<DataAccount[]>([]);
  const [loading, setLoading] = useState(false);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [selectedAccount, setSelectedAccount] = useState<DataAccount | null>(null);
  const [testingConnection, setTestingConnection] = useState<string | null>(null);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');

  // Form state
  const [formData, setFormData] = useState({
    data_account_id: '',
    name: '',
    description: '',
    role_arn: '',
    external_id: '',
    region: 'us-east-1',
  });

  useEffect(() => {
    loadDataAccounts();
  }, []);

  const loadDataAccounts = async () => {
    setLoading(true);
    try {
      const response = await apiService.listDataAccounts();
      setDataAccounts(response.data_accounts || []);
    } catch (err: any) {
      setError(err.message || 'Failed to load Data Accounts');
    } finally {
      setLoading(false);
    }
  };

  const handleAdd = async () => {
    setError('');
    setLoading(true);
    try {
      await apiService.createDataAccount(formData);
      setSuccess('Data Account registered successfully');
      setShowAddModal(false);
      resetForm();
      loadDataAccounts();
    } catch (err: any) {
      setError(err.message || 'Failed to register Data Account');
    } finally {
      setLoading(false);
    }
  };

  const handleEdit = async () => {
    if (!selectedAccount) return;
    
    setError('');
    setLoading(true);
    try {
      await apiService.updateDataAccount(selectedAccount.data_account_id, formData);
      setSuccess('Data Account updated successfully');
      setShowEditModal(false);
      setSelectedAccount(null);
      resetForm();
      loadDataAccounts();
    } catch (err: any) {
      setError(err.message || 'Failed to update Data Account');
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (accountId: string) => {
    if (!confirm('Are you sure you want to delete this Data Account?')) return;
    
    setError('');
    setLoading(true);
    try {
      await apiService.deleteDataAccount(accountId);
      setSuccess('Data Account deleted successfully');
      loadDataAccounts();
    } catch (err: any) {
      setError(err.message || 'Failed to delete Data Account');
    } finally {
      setLoading(false);
    }
  };

  const handleTestConnection = async (accountId: string) => {
    setTestingConnection(accountId);
    setError('');
    try {
      const response = await apiService.testDataAccountConnection(accountId);
      if (response.result.status === 'success') {
        setSuccess('Connection test successful');
      } else {
        setError(`Connection test failed: ${response.result.error}`);
      }
      loadDataAccounts();
    } catch (err: any) {
      setError(err.message || 'Failed to test connection');
    } finally {
      setTestingConnection(null);
    }
  };

  const openEditModal = (account: DataAccount) => {
    setSelectedAccount(account);
    setFormData({
      data_account_id: account.data_account_id,
      name: account.name,
      description: account.description || '',
      role_arn: account.role_arn,
      external_id: account.external_id,
      region: account.region,
    });
    setShowEditModal(true);
  };

  const resetForm = () => {
    setFormData({
      data_account_id: '',
      name: '',
      description: '',
      role_arn: '',
      external_id: '',
      region: 'us-east-1',
    });
  };

  const DataAccountsTab = () => (
    <SpaceBetween size="l">
      {error && <Alert type="error" dismissible onDismiss={() => setError('')}>{error}</Alert>}
      {success && <Alert type="success" dismissible onDismiss={() => setSuccess('')}>{success}</Alert>}

      <Table
        items={dataAccounts}
        loading={loading}
        loadingText="Loading Data Accounts..."
        header={
          <Header
            variant="h2"
            actions={
              <Button variant="primary" onClick={() => setShowAddModal(true)}>
                Add Data Account
              </Button>
            }
          >
            Data Accounts
          </Header>
        }
        columnDefinitions={[
          {
            id: 'name',
            header: 'Name',
            cell: item => (
              <SpaceBetween direction="vertical" size="xxs">
                <Box fontWeight="bold">{item.name}</Box>
                <Box color="text-body-secondary" fontSize="body-s">{item.description}</Box>
              </SpaceBetween>
            ),
          },
          {
            id: 'account',
            header: 'Account ID',
            cell: item => item.data_account_id,
          },
          {
            id: 'region',
            header: 'Region',
            cell: item => item.region,
          },
          {
            id: 'status',
            header: 'Status',
            cell: item => {
              const test = item.connection_test;
              if (!test) {
                return <Badge color="grey">Not tested</Badge>;
              }
              return test.status === 'success' ? (
                <StatusIndicator type="success">Connected</StatusIndicator>
              ) : (
                <StatusIndicator type="error">Failed</StatusIndicator>
              );
            },
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: item => (
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="icon"
                  iconName="status-positive"
                  onClick={() => handleTestConnection(item.data_account_id)}
                  loading={testingConnection === item.data_account_id}
                  ariaLabel="Test connection"
                />
                <Button
                  variant="icon"
                  iconName="edit"
                  onClick={() => openEditModal(item)}
                  ariaLabel="Edit"
                />
                <Button
                  variant="icon"
                  iconName="remove"
                  onClick={() => handleDelete(item.data_account_id)}
                  ariaLabel="Delete"
                />
              </SpaceBetween>
            ),
          },
        ]}
        empty={
          <Box textAlign="center" color="inherit">
            <Box padding={{ bottom: 's' }} variant="p" color="inherit">
              No Data Accounts registered
            </Box>
            <Button onClick={() => setShowAddModal(true)}>Add Data Account</Button>
          </Box>
        }
      />
    </SpaceBetween>
  );

  const handleFileUpload = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    if (file) {
      const reader = new FileReader();
      reader.onload = (e) => {
        const content = e.target?.result as string;
        const lines = content.split('\n');
        const config: Record<string, string> = {};
        
        lines.forEach(line => {
          const match = line.match(/^([^:]+):\s*(.+)$/);
          if (match) {
            config[match[1].trim()] = match[2].trim();
          }
        });
        
        // Auto-fill form from config file
        setFormData({
          data_account_id: config['Data Account ID'] || formData.data_account_id,
          name: formData.name || 'Production Data Account', // Keep user's name if already entered
          description: formData.description || 'Centralized training data storage',
          role_arn: config['Portal Access Role ARN'] || formData.role_arn,
          external_id: config['External ID'] || formData.external_id,
          region: formData.region || 'us-east-1',
        });
        
        setSuccess('Configuration file loaded successfully');
      };
      reader.readAsText(file);
    }
  };

  const DataAccountForm = () => (
    <Form>
      <SpaceBetween size="m">
        {!selectedAccount && (
          <FormField
            label="Upload Configuration File"
            description="Upload data-account-config.txt to auto-fill fields"
            stretch
          >
            <input
              type="file"
              accept=".txt"
              onChange={handleFileUpload}
              style={{
                padding: '8px',
                border: '1px dashed #aab7b8',
                borderRadius: '4px',
                width: '100%',
                cursor: 'pointer',
              }}
            />
          </FormField>
        )}

        {!selectedAccount && (
          <Box textAlign="center" color="text-body-secondary">
            <Box variant="small">or enter manually below</Box>
          </Box>
        )}

        <FormField label="Account ID" constraintText="AWS Account ID">
          <Input
            value={formData.data_account_id}
            onChange={({ detail }) => setFormData({ ...formData, data_account_id: detail.value })}
            placeholder="123456789012"
            disabled={!!selectedAccount}
          />
        </FormField>

        <FormField label="Name" constraintText="Friendly name for this Data Account">
          <Input
            value={formData.name}
            onChange={({ detail }) => setFormData({ ...formData, name: detail.value })}
            placeholder="Production Data Account"
          />
        </FormField>

        <FormField label="Description" constraintText="Optional description">
          <Textarea
            value={formData.description}
            onChange={({ detail }) => setFormData({ ...formData, description: detail.value })}
            placeholder="Centralized data storage for production usecases"
            rows={2}
          />
        </FormField>

        <FormField label="Role ARN" constraintText="IAM role ARN in the Data Account">
          <Input
            value={formData.role_arn}
            onChange={({ detail }) => setFormData({ ...formData, role_arn: detail.value })}
            placeholder="arn:aws:iam::123456789012:role/DDAPortalAccessRole"
          />
        </FormField>

        <FormField label="External ID" constraintText="External ID for role assumption">
          <Input
            value={formData.external_id}
            onChange={({ detail }) => setFormData({ ...formData, external_id: detail.value })}
            placeholder="7B1EA7C8-A279-4F44-9732-E1C912F01272"
            type="password"
          />
        </FormField>

        <FormField label="Region">
          <Input
            value={formData.region}
            onChange={({ detail }) => setFormData({ ...formData, region: detail.value })}
            placeholder="us-east-1"
          />
        </FormField>
      </SpaceBetween>
    </Form>
  );

  return (
    <Container>
      <SpaceBetween size="l">
        <Header variant="h1">Portal Settings</Header>

        <Tabs
          activeTabId={activeTab}
          onChange={({ detail }) => setActiveTab(detail.activeTabId)}
          tabs={[
            {
              id: 'data-accounts',
              label: 'Data Accounts',
              content: <DataAccountsTab />,
            },
            {
              id: 'general',
              label: 'General',
              content: (
                <Box padding="l">
                  <Alert type="info">
                    General settings coming soon
                  </Alert>
                </Box>
              ),
            },
          ]}
        />

        {/* Add Data Account Modal */}
        <Modal
          visible={showAddModal}
          onDismiss={() => {
            setShowAddModal(false);
            resetForm();
          }}
          header="Add Data Account"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowAddModal(false);
                    resetForm();
                  }}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleAdd}
                  loading={loading}
                  disabled={
                    !formData.data_account_id ||
                    !formData.name ||
                    !formData.role_arn ||
                    !formData.external_id
                  }
                >
                  Register
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <DataAccountForm />
        </Modal>

        {/* Edit Data Account Modal */}
        <Modal
          visible={showEditModal}
          onDismiss={() => {
            setShowEditModal(false);
            setSelectedAccount(null);
            resetForm();
          }}
          header="Edit Data Account"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowEditModal(false);
                    setSelectedAccount(null);
                    resetForm();
                  }}
                >
                  Cancel
                </Button>
                <Button variant="primary" onClick={handleEdit} loading={loading}>
                  Save Changes
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <DataAccountForm />
        </Modal>
      </SpaceBetween>
    </Container>
  );
}
