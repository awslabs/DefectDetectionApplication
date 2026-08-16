/**
 * Labeling team management page (dda-data-labeling task 16.2, route
 * `/labeling/teams`).
 *
 * Cloudscape table of the Labeling_Teams scoped to the selected Use_Case,
 * each with its member list (identity + email, Requirement 3.8); a
 * create-team form whose name validation mirrors the backend (non-empty,
 * at most 128 characters, Requirement 3.1); an add-member modal listing
 * the portal users holding the Data_Labeler role via the existing user
 * administration listing API (Requirement 3.3); member removal behind a
 * confirmation explaining the reassignment consequence — the member's
 * unsubmitted tasks in in-progress jobs are redistributed to the
 * remaining members (Requirement 3.6); and team deletion (rejected
 * server-side while an in-progress job references the team).
 *
 * The page is UI-gated to UseCaseAdmin/PortalAdmin through `RequireRole`
 * in App.tsx; server-side RBAC (`labeling-teams:manage`) remains the
 * ultimate authority (Requirement 3.7).
 */

import { useState, useEffect, useCallback } from 'react';
import {
  Container,
  Header,
  Table,
  Button,
  SpaceBetween,
  Box,
  Select,
  SelectProps,
  Alert,
  Modal,
  Form,
  FormField,
  Input,
} from '@cloudscape-design/components';
import { UseCase } from '../../types';
import {
  apiService,
  LabelingTeam,
  LabelingTeamMember,
  AdminAccount,
} from '../../services/api';
import { getErrorMessage } from '../../utils/errorHandling';

/** Mirrors the backend's TEAM_NAME_MAX_LENGTH (Requirement 3.2). */
const TEAM_NAME_MAX_LENGTH = 128;

/**
 * Client-side team name validation mirroring dda_labeling.py
 * create_labeling_team (Requirement 3.2): non-empty after trimming and at
 * most 128 characters. Returns the error text or null when valid.
 */
export function validateTeamName(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) {
    return 'Team name must not be empty';
  }
  if (trimmed.length > TEAM_NAME_MAX_LENGTH) {
    return `Team name must be at most ${TEAM_NAME_MAX_LENGTH} characters`;
  }
  return null;
}

export default function LabelingTeams() {
  const [useCases, setUseCases] = useState<UseCase[]>([]);
  const [selectedUseCase, setSelectedUseCase] =
    useState<SelectProps.Option | null>(null);
  const [teams, setTeams] = useState<LabelingTeam[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Create-team modal state.
  const [showCreateModal, setShowCreateModal] = useState(false);
  const [newTeamName, setNewTeamName] = useState('');
  const [nameTouched, setNameTouched] = useState(false);
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  // Add-member modal state.
  const [addMemberTeam, setAddMemberTeam] = useState<LabelingTeam | null>(null);
  const [labelerAccounts, setLabelerAccounts] = useState<AdminAccount[]>([]);
  const [labelersLoading, setLabelersLoading] = useState(false);
  const [selectedLabeler, setSelectedLabeler] =
    useState<SelectProps.Option | null>(null);
  const [addingMember, setAddingMember] = useState(false);
  const [addMemberError, setAddMemberError] = useState<string | null>(null);

  // Remove-member confirmation state.
  const [removal, setRemoval] = useState<{
    team: LabelingTeam;
    member: LabelingTeamMember;
  } | null>(null);
  const [removingMember, setRemovingMember] = useState(false);
  const [removeError, setRemoveError] = useState<string | null>(null);

  // Delete-team confirmation state.
  const [teamToDelete, setTeamToDelete] = useState<LabelingTeam | null>(null);
  const [deletingTeam, setDeletingTeam] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  useEffect(() => {
    const loadUseCases = async () => {
      try {
        const response = await apiService.listUseCases();
        const useCaseList = response.usecases || [];
        setUseCases(useCaseList);
        if (useCaseList.length > 0) {
          setSelectedUseCase({
            label: useCaseList[0].name,
            value: useCaseList[0].usecase_id,
          });
        } else {
          setLoading(false);
        }
      } catch (err) {
        console.error('Failed to load use cases:', err);
        setError(getErrorMessage(err, 'Failed to load use cases'));
        setLoading(false);
      }
    };
    loadUseCases();
  }, []);

  const loadTeams = useCallback(async () => {
    if (!selectedUseCase?.value) {
      setTeams([]);
      setLoading(false);
      return;
    }
    try {
      setLoading(true);
      setError(null);
      const response = await apiService.listLabelingTeams(
        selectedUseCase.value
      );
      setTeams(response.teams || []);
    } catch (err) {
      console.error('Failed to load labeling teams:', err);
      setError(getErrorMessage(err, 'Failed to load labeling teams'));
      setTeams([]);
    } finally {
      setLoading(false);
    }
  }, [selectedUseCase]);

  useEffect(() => {
    loadTeams();
  }, [loadTeams]);

  const nameValidationError = validateTeamName(newTeamName);

  const createTeam = async () => {
    if (!selectedUseCase?.value || nameValidationError) return;
    try {
      setCreating(true);
      setCreateError(null);
      await apiService.createLabelingTeam({
        usecase_id: selectedUseCase.value,
        team_name: newTeamName.trim(),
      });
      setShowCreateModal(false);
      setNewTeamName('');
      setNameTouched(false);
      await loadTeams();
    } catch (err) {
      // Duplicate names within the use case are rejected server-side
      // (Requirement 3.2) — surface the backend's message.
      setCreateError(getErrorMessage(err, 'Failed to create labeling team'));
    } finally {
      setCreating(false);
    }
  };

  const openAddMemberModal = async (team: LabelingTeam) => {
    setAddMemberTeam(team);
    setSelectedLabeler(null);
    setAddMemberError(null);
    setLabelersLoading(true);
    try {
      // Reuse the existing user administration listing (user_admin.py):
      // the add-member candidates are the accounts holding the
      // Data_Labeler role (Requirements 3.3, 3.4).
      const response = await apiService.listAdminUsers();
      setLabelerAccounts(
        (response.users || []).filter((account) => account.role === 'DataLabeler')
      );
    } catch (err) {
      console.error('Failed to load Data_Labeler users:', err);
      setLabelerAccounts([]);
      setAddMemberError(
        getErrorMessage(err, 'Failed to load users with the Data Labeler role')
      );
    } finally {
      setLabelersLoading(false);
    }
  };

  const addMember = async () => {
    if (!addMemberTeam || !selectedLabeler?.value) return;
    try {
      setAddingMember(true);
      setAddMemberError(null);
      await apiService.addTeamMember(
        addMemberTeam.team_id,
        selectedLabeler.value
      );
      setAddMemberTeam(null);
      setSelectedLabeler(null);
      await loadTeams();
    } catch (err) {
      // Missing Data_Labeler role or duplicate membership rejections
      // (Requirements 3.4, 3.5) arrive here — surface the message.
      setAddMemberError(getErrorMessage(err, 'Failed to add team member'));
    } finally {
      setAddingMember(false);
    }
  };

  const removeMember = async () => {
    if (!removal) return;
    try {
      setRemovingMember(true);
      setRemoveError(null);
      await apiService.removeTeamMember(
        removal.team.team_id,
        removal.member.user_id
      );
      setRemoval(null);
      await loadTeams();
    } catch (err) {
      setRemoveError(getErrorMessage(err, 'Failed to remove team member'));
    } finally {
      setRemovingMember(false);
    }
  };

  const deleteTeam = async () => {
    if (!teamToDelete) return;
    try {
      setDeletingTeam(true);
      setDeleteError(null);
      await apiService.deleteLabelingTeam(teamToDelete.team_id);
      setTeamToDelete(null);
      await loadTeams();
    } catch (err) {
      // A 409 means an in-progress labeling job still references the team.
      setDeleteError(getErrorMessage(err, 'Failed to delete labeling team'));
    } finally {
      setDeletingTeam(false);
    }
  };

  // Accounts already on the team are excluded from the add-member options
  // (memberships store the Cognito sub; accounts are matched by email).
  const memberEmails = new Set(
    (addMemberTeam?.members || []).map((member) => member.email)
  );
  const labelerOptions: SelectProps.Option[] = labelerAccounts
    .filter((account) => !memberEmails.has(account.email))
    .map((account) => ({
      label: account.username,
      value: account.username,
      description: account.email,
    }));

  return (
    <Container
      header={
        <Header
          variant="h1"
          description={
            <SpaceBetween direction="horizontal" size="m" alignItems="center">
              <Box variant="span">Use Case:</Box>
              <Select
                selectedOption={selectedUseCase}
                onChange={({ detail }) =>
                  setSelectedUseCase(detail.selectedOption)
                }
                options={useCases.map((uc) => ({
                  label: uc.name,
                  value: uc.usecase_id,
                }))}
                placeholder="Select a use case"
                disabled={useCases.length === 0}
                expandToViewport
              />
            </SpaceBetween>
          }
          actions={
            <Button
              variant="primary"
              onClick={() => {
                setNewTeamName('');
                setNameTouched(false);
                setCreateError(null);
                setShowCreateModal(true);
              }}
              disabled={!selectedUseCase}
            >
              Create Team
            </Button>
          }
        >
          Labeling Teams
        </Header>
      }
    >
      <SpaceBetween size="l">
        {error && (
          <Alert type="error" dismissible onDismiss={() => setError(null)}>
            {error}
          </Alert>
        )}

        <Table
          resizableColumns
          columnDefinitions={[
            {
              id: 'team_name',
              header: 'Team Name',
              cell: (item: LabelingTeam) => item.team_name,
            },
            {
              id: 'members',
              header: 'Members',
              cell: (item: LabelingTeam) =>
                item.members.length === 0 ? (
                  <Box color="text-body-secondary">No members</Box>
                ) : (
                  <SpaceBetween size="xxs">
                    {item.members.map((member) => (
                      <SpaceBetween
                        key={member.user_id}
                        direction="horizontal"
                        size="xs"
                        alignItems="center"
                      >
                        <Box variant="span">
                          {member.email || member.user_id}
                        </Box>
                        <Button
                          variant="inline-link"
                          ariaLabel={`Remove ${member.email || member.user_id} from ${item.team_name}`}
                          onClick={() => {
                            setRemoveError(null);
                            setRemoval({ team: item, member });
                          }}
                        >
                          Remove
                        </Button>
                      </SpaceBetween>
                    ))}
                  </SpaceBetween>
                ),
            },
            {
              id: 'created_at',
              header: 'Created',
              cell: (item: LabelingTeam) =>
                item.created_at
                  ? new Date(item.created_at).toLocaleString()
                  : '-',
            },
            {
              id: 'actions',
              header: 'Actions',
              cell: (item: LabelingTeam) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    variant="inline-link"
                    onClick={() => openAddMemberModal(item)}
                  >
                    Add Member
                  </Button>
                  <Button
                    variant="inline-link"
                    onClick={() => {
                      setDeleteError(null);
                      setTeamToDelete(item);
                    }}
                  >
                    Delete
                  </Button>
                </SpaceBetween>
              ),
            },
          ]}
          items={teams}
          loading={loading}
          loadingText="Loading labeling teams"
          empty={
            <Box textAlign="center" color="inherit">
              <b>No labeling teams</b>
              <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                {selectedUseCase
                  ? 'No labeling teams found for this use case.'
                  : 'Select a use case to view its labeling teams.'}
              </Box>
              {selectedUseCase && (
                <Button
                  onClick={() => {
                    setNewTeamName('');
                    setNameTouched(false);
                    setCreateError(null);
                    setShowCreateModal(true);
                  }}
                >
                  Create Team
                </Button>
              )}
            </Box>
          }
        />

        {/* Create-team modal: name validation mirrors the backend
            (Requirements 3.1, 3.2). */}
        <Modal
          visible={showCreateModal}
          onDismiss={() => setShowCreateModal(false)}
          header="Create Labeling Team"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setShowCreateModal(false)}>
                  Cancel
                </Button>
                <Button
                  variant="primary"
                  onClick={createTeam}
                  loading={creating}
                  disabled={!!nameValidationError || creating}
                >
                  Create
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <Form>
            <SpaceBetween size="l">
              {createError && <Alert type="error">{createError}</Alert>}
              <FormField
                label="Team name"
                description={`Must not be empty and can be at most ${TEAM_NAME_MAX_LENGTH} characters. Names must be unique within the use case.`}
                errorText={nameTouched ? nameValidationError : null}
                stretch
              >
                <Input
                  value={newTeamName}
                  onChange={({ detail }) => {
                    setNewTeamName(detail.value);
                    setNameTouched(true);
                  }}
                  placeholder="Enter team name"
                  autoFocus
                />
              </FormField>
            </SpaceBetween>
          </Form>
        </Modal>

        {/* Add-member modal: candidates are the portal users holding the
            Data_Labeler role (Requirements 3.3, 3.4). */}
        <Modal
          visible={!!addMemberTeam}
          onDismiss={() => setAddMemberTeam(null)}
          header={`Add Member to ${addMemberTeam?.team_name ?? ''}`}
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setAddMemberTeam(null)}>Cancel</Button>
                <Button
                  variant="primary"
                  onClick={addMember}
                  loading={addingMember}
                  disabled={!selectedLabeler || addingMember}
                >
                  Add Member
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="l">
            {addMemberError && <Alert type="error">{addMemberError}</Alert>}
            <FormField
              label="User"
              description="Only portal users holding the Data Labeler role can join a labeling team."
              stretch
            >
              <Select
                selectedOption={selectedLabeler}
                onChange={({ detail }) =>
                  setSelectedLabeler(detail.selectedOption)
                }
                options={labelerOptions}
                statusType={labelersLoading ? 'loading' : 'finished'}
                loadingText="Loading Data Labeler users"
                placeholder="Select a Data Labeler user"
                empty="No users with the Data Labeler role are available."
                expandToViewport
              />
            </FormField>
          </SpaceBetween>
        </Modal>

        {/* Remove-member confirmation explaining the reassignment
            consequence (Requirements 3.6, 5.3). */}
        <Modal
          visible={!!removal}
          onDismiss={() => setRemoval(null)}
          header="Remove Team Member"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setRemoval(null)}>Cancel</Button>
                <Button
                  variant="primary"
                  onClick={removeMember}
                  loading={removingMember}
                >
                  Remove Member
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {removeError && <Alert type="error">{removeError}</Alert>}
            <Box variant="p">
              Remove{' '}
              <strong>
                {removal?.member.email || removal?.member.user_id}
              </strong>{' '}
              from <strong>{removal?.team.team_name}</strong>?
            </Box>
            <Alert type="warning">
              The member's unsubmitted labeling tasks in in-progress jobs
              will be redistributed across the remaining team members.
              Their already submitted labels are kept unchanged. If they are
              the last member, the affected jobs are blocked until a new
              member is added.
            </Alert>
          </SpaceBetween>
        </Modal>

        {/* Delete-team confirmation. Deletion is rejected server-side while
            an in-progress labeling job references the team. */}
        <Modal
          visible={!!teamToDelete}
          onDismiss={() => setTeamToDelete(null)}
          header="Delete Labeling Team"
          footer={
            <Box float="right">
              <SpaceBetween direction="horizontal" size="xs">
                <Button onClick={() => setTeamToDelete(null)}>Cancel</Button>
                <Button
                  variant="primary"
                  onClick={deleteTeam}
                  loading={deletingTeam}
                >
                  Delete Team
                </Button>
              </SpaceBetween>
            </Box>
          }
        >
          <SpaceBetween size="m">
            {deleteError && <Alert type="error">{deleteError}</Alert>}
            <Box variant="p">
              Delete <strong>{teamToDelete?.team_name}</strong> and its
              membership? Teams referenced by an in-progress labeling job
              cannot be deleted.
            </Box>
          </SpaceBetween>
        </Modal>
      </SpaceBetween>
    </Container>
  );
}
