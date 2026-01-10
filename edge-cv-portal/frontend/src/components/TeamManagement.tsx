import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  Modal,
  Box,
  SpaceBetween,
  Table,
  Button,
  FormField,
  Input,
  Select,
  Alert,
  Header,
  Badge,
  StatusIndicator,
} from '@cloudscape-design/components';
import { apiService } from '../services/api';

interface TeamManagementProps {
  visible: boolean;
  onDismiss: () => void;
  usecaseId: string;
  usecaseName: string;
}

interface TeamMember {
  user_id: string;
  role: string;
  assigned_at?: number;
  assigned_by?: string;
}

const ROLE_OPTIONS = [
  { label: 'Viewer', value: 'Viewer', description: 'Read-only access' },
  { label: 'Operator', value: 'Operator', description: 'Deploy and manage devices' },
  { label: 'Data Scientist', value: 'DataScientist', description: 'Labeling, training, models' },
  { label: 'UseCase Admin', value: 'UseCaseAdmin', description: 'Full access to this usecase' },
];

const getRoleBadgeColor = (role: string): 'blue' | 'green' | 'grey' | 'red' => {
  switch (role) {
    case 'UseCaseAdmin':
      return 'red';
    case 'DataScientist':
      return 'blue';
    case 'Operator':
      return 'green';
    default:
      return 'grey';
  }
};

export default function TeamManagement({
  visible,
  onDismiss,
  usecaseId,
  usecaseName,
}: TeamManagementProps) {
  const queryClient = useQueryClient();
  const [showAddMember, setShowAddMember] = useState(false);
  const [newMemberEmail, setNewMemberEmail] = useState('');
  const [newMemberRole, setNewMemberRole] = useState<{ label: string; value: string } | null>(null);
  const [error, setError] = useState('');
  const [successMessage, setSuccessMessage] = useState('');

  // Fetch team members
  const { data, isLoading, refetch } = useQuery({
    queryKey: ['usecase-users', usecaseId],
    queryFn: () => apiService.listUsecaseUsers(usecaseId),
    enabled: visible && !!usecaseId,
  });

  // Transform data to flat list of team members
  const teamMembers: TeamMember[] = (data?.users || []).flatMap((user) =>
    user.roles
      .filter((r) => r.usecase_id === usecaseId)
      .map((r) => ({
        user_id: user.user_id,
        role: r.role,
        assigned_at: r.assigned_at,
        assigned_by: r.assigned_by,
      }))
  );

  // Add member mutation
  const addMemberMutation = useMutation({
    mutationFn: (data: { user_id: string; usecase_id: string; role: string }) =>
      apiService.assignUserRole(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['usecase-users', usecaseId] });
      setShowAddMember(false);
      setNewMemberEmail('');
      setNewMemberRole(null);
      setSuccessMessage('Team member added successfully');
      setTimeout(() => setSuccessMessage(''), 5000);
      refetch();
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  // Remove member mutation
  const removeMemberMutation = useMutation({
    mutationFn: (userId: string) => apiService.removeUserRole(userId, usecaseId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['usecase-users', usecaseId] });
      setSuccessMessage('Team member removed successfully');
      setTimeout(() => setSuccessMessage(''), 5000);
      refetch();
    },
    onError: (err: Error) => {
      setError(err.message);
    },
  });

  const handleAddMember = () => {
    if (!newMemberEmail || !newMemberRole) {
      setError('Email and role are required');
      return;
    }
    setError('');
    addMemberMutation.mutate({
      user_id: newMemberEmail, // Using email as user_id
      usecase_id: usecaseId,
      role: newMemberRole.value,
    });
  };

  const handleRemoveMember = (userId: string) => {
    if (window.confirm(`Remove ${userId} from this usecase?`)) {
      removeMemberMutation.mutate(userId);
    }
  };

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={
        <Header
          variant="h2"
          description={`Manage who has access to "${usecaseName}"`}
        >
          Team Members
        </Header>
      }
      size="large"
      footer={
        <Box float="right">
          <Button variant="link" onClick={onDismiss}>
            Close
          </Button>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError('')}>
            {error}
          </Alert>
        )}

        {successMessage && (
          <Alert type="success" dismissible onDismiss={() => setSuccessMessage('')}>
            {successMessage}
          </Alert>
        )}

        {/* Add Member Section */}
        {showAddMember ? (
          <Box padding="m" variant="div">
            <SpaceBetween size="m">
              <Header variant="h3">Add Team Member</Header>
              <FormField
                label="User Email"
                description="Enter the email address of the user to add"
              >
                <Input
                  value={newMemberEmail}
                  onChange={({ detail }) => setNewMemberEmail(detail.value)}
                  placeholder="user@example.com"
                  type="email"
                />
              </FormField>
              <FormField label="Role" description="Select the role for this user">
                <Select
                  selectedOption={newMemberRole}
                  onChange={({ detail }) =>
                    setNewMemberRole(detail.selectedOption as { label: string; value: string })
                  }
                  options={ROLE_OPTIONS}
                  placeholder="Select a role"
                />
              </FormField>
              <SpaceBetween direction="horizontal" size="xs">
                <Button
                  variant="link"
                  onClick={() => {
                    setShowAddMember(false);
                    setNewMemberEmail('');
                    setNewMemberRole(null);
                    setError('');
                  }}
                >
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={handleAddMember}
                  loading={addMemberMutation.isPending}
                >
                  Add Member
                </Button>
              </SpaceBetween>
            </SpaceBetween>
          </Box>
        ) : (
          <Button onClick={() => setShowAddMember(true)} iconName="add-plus">
            Add Team Member
          </Button>
        )}

        {/* Team Members Table */}
        <Table
          loading={isLoading}
          items={teamMembers}
          columnDefinitions={[
            {
              id: 'user_id',
              header: 'User',
              cell: (item) => item.user_id,
              sortingField: 'user_id',
            },
            {
              id: 'role',
              header: 'Role',
              cell: (item) => (
                <Badge color={getRoleBadgeColor(item.role)}>{item.role}</Badge>
              ),
              sortingField: 'role',
            },
            {
              id: 'assigned_at',
              header: 'Added',
              cell: (item) =>
                item.assigned_at
                  ? new Date(item.assigned_at * 1000).toLocaleDateString()
                  : '-',
            },
            {
              id: 'assigned_by',
              header: 'Added By',
              cell: (item) => item.assigned_by || '-',
            },
            {
              id: 'actions',
              header: 'Actions',
              cell: (item) => (
                <Button
                  variant="link"
                  onClick={() => handleRemoveMember(item.user_id)}
                  loading={removeMemberMutation.isPending}
                >
                  Remove
                </Button>
              ),
            },
          ]}
          empty={
            <Box textAlign="center" color="inherit" padding="l">
              <SpaceBetween size="m">
                <b>No team members</b>
                <Box variant="p" color="inherit">
                  Add team members to give them access to this usecase.
                </Box>
              </SpaceBetween>
            </Box>
          }
          header={
            <Header
              variant="h3"
              counter={`(${teamMembers.length})`}
              description="Users with access to this usecase"
            >
              Current Team
            </Header>
          }
        />

        {/* Role Descriptions */}
        <Box variant="div" padding="s">
          <Header variant="h3">Role Descriptions</Header>
          <SpaceBetween size="xs">
            <Box>
              <StatusIndicator type="info">
                <strong>Viewer:</strong> Read-only access to view usecases, jobs, models, and deployments
              </StatusIndicator>
            </Box>
            <Box>
              <StatusIndicator type="info">
                <strong>Operator:</strong> Can create deployments, manage devices, and view all resources
              </StatusIndicator>
            </Box>
            <Box>
              <StatusIndicator type="info">
                <strong>Data Scientist:</strong> Can create labeling jobs, training jobs, and manage models
              </StatusIndicator>
            </Box>
            <Box>
              <StatusIndicator type="info">
                <strong>UseCase Admin:</strong> Full access including managing team members
              </StatusIndicator>
            </Box>
          </SpaceBetween>
        </Box>
      </SpaceBetween>
    </Modal>
  );
}
