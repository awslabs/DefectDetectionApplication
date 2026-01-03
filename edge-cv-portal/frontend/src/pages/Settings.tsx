import { useState } from 'react';
import {
  Container,
  Header,
  Tabs,
  SpaceBetween,
  Box,
  Button,
  Table,
  Modal,
  FormField,
  Input,
  Select,
  SelectProps,
  Alert,
} from '@cloudscape-design/components';
import ConfirmationModal from '../components/ConfirmationModal';

interface CompilationTarget {
  id: string;
  name: string;
  platform: string;
  arch: string;
  compiler: string;
  compiler_options: Record<string, string>;
}

export default function Settings() {
  const [activeTabId, setActiveTabId] = useState('compilation');
  const [targets, setTargets] = useState<CompilationTarget[]>([
    {
      id: '1',
      name: 'Jetson Nano',
      platform: 'jetson',
      arch: 'aarch64',
      compiler: 'neo',
      compiler_options: { target_platform: 'jetson_nano' },
    },
    {
      id: '2',
      name: 'x86 CPU',
      platform: 'x86',
      arch: 'x86_64',
      compiler: 'neo',
      compiler_options: { target_platform: 'ml_c5' },
    },
    {
      id: '3',
      name: 'ARM64 CPU',
      platform: 'arm',
      arch: 'aarch64',
      compiler: 'neo',
      compiler_options: { target_platform: 'ml_m6g' },
    },
  ]);
  const [selectedItems, setSelectedItems] = useState<CompilationTarget[]>([]);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showEditModal, setShowEditModal] = useState(false);
  const [showDeleteModal, setShowDeleteModal] = useState(false);
  const [editingTarget, setEditingTarget] = useState<CompilationTarget | null>(null);
  
  // Form state
  const [targetName, setTargetName] = useState('');
  const [platform, setPlatform] = useState<SelectProps.Option | null>(null);
  const [arch, setArch] = useState<SelectProps.Option | null>(null);
  const [targetPlatform, setTargetPlatform] = useState('');
  const [saving, setSaving] = useState(false);

  const platformOptions: SelectProps.Option[] = [
    { label: 'Jetson', value: 'jetson' },
    { label: 'x86', value: 'x86' },
    { label: 'ARM', value: 'arm' },
  ];

  const archOptions: SelectProps.Option[] = [
    { label: 'x86_64', value: 'x86_64' },
    { label: 'aarch64', value: 'aarch64' },
  ];

  const handleAdd = () => {
    setTargetName('');
    setPlatform(null);
    setArch(null);
    setTargetPlatform('');
    setShowAddModal(true);
  };

  const handleEdit = () => {
    if (selectedItems.length > 0) {
      const target = selectedItems[0];
      setEditingTarget(target);
      setTargetName(target.name);
      setPlatform(platformOptions.find(p => p.value === target.platform) || null);
      setArch(archOptions.find(a => a.value === target.arch) || null);
      setTargetPlatform(target.compiler_options.target_platform);
      setShowEditModal(true);
    }
  };

  const handleDelete = () => {
    setShowDeleteModal(true);
  };

  const handleSaveAdd = async () => {
    setSaving(true);
    try {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const newTarget: CompilationTarget = {
        id: String(targets.length + 1),
        name: targetName,
        platform: platform?.value || '',
        arch: arch?.value || '',
        compiler: 'neo',
        compiler_options: { target_platform: targetPlatform },
      };
      setTargets([...targets, newTarget]);
      setShowAddModal(false);
    } catch (error) {
      console.error('Failed to add target:', error);
    } finally {
      setSaving(false);
    }
  };

  const handleSaveEdit = async () => {
    setSaving(true);
    try {
      await new Promise(resolve => setTimeout(resolve, 1000));
      setTargets(targets.map(t => 
        t.id === editingTarget?.id
          ? {
              ...t,
              name: targetName,
              platform: platform?.value || '',
              arch: arch?.value || '',
              compiler_options: { target_platform: targetPlatform },
            }
          : t
      ));
      setShowEditModal(false);
      setSelectedItems([]);
    } catch (error) {
      console.error('Failed to update target:', error);
    } finally {
      setSaving(false);
    }
  };

  const handleConfirmDelete = async () => {
    setSaving(true);
    try {
      await new Promise(resolve => setTimeout(resolve, 1000));
      const idsToDelete = selectedItems.map(item => item.id);
      setTargets(targets.filter(t => !idsToDelete.includes(t.id)));
      setShowDeleteModal(false);
      setSelectedItems([]);
    } catch (error) {
      console.error('Failed to delete targets:', error);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Container
        header={
          <Header variant="h1" description="Configure portal settings">
            Settings
          </Header>
        }
      >
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: 'compilation',
              label: 'Compilation Targets',
              content: (
                <SpaceBetween size="l">
                  <Alert type="info">
                    Compilation targets define the hardware platforms that models can be compiled for.
                    These targets are used during the model compilation step.
                  </Alert>

                  <Table
                    columnDefinitions={[
                      {
                        id: 'name',
                        header: 'Name',
                        cell: item => item.name,
                      },
                      {
                        id: 'platform',
                        header: 'Platform',
                        cell: item => item.platform,
                      },
                      {
                        id: 'arch',
                        header: 'Architecture',
                        cell: item => item.arch,
                      },
                      {
                        id: 'target_platform',
                        header: 'Target Platform',
                        cell: item => item.compiler_options.target_platform,
                      },
                    ]}
                    items={targets}
                    selectionType="single"
                    selectedItems={selectedItems}
                    onSelectionChange={({ detail }) => setSelectedItems(detail.selectedItems)}
                    header={
                      <Header
                        actions={
                          <SpaceBetween direction="horizontal" size="xs">
                            <Button onClick={handleAdd}>Add Target</Button>
                            <Button onClick={handleEdit} disabled={selectedItems.length === 0}>
                              Edit
                            </Button>
                            <Button onClick={handleDelete} disabled={selectedItems.length === 0}>
                              Delete
                            </Button>
                          </SpaceBetween>
                        }
                      >
                        Compilation Targets
                      </Header>
                    }
                    empty={
                      <Box textAlign="center" color="inherit">
                        <b>No compilation targets</b>
                        <Box padding={{ bottom: 's' }} variant="p" color="inherit">
                          No compilation targets configured.
                        </Box>
                        <Button onClick={handleAdd}>Add Target</Button>
                      </Box>
                    }
                  />
                </SpaceBetween>
              ),
            },
            {
              id: 'general',
              label: 'General',
              content: (
                <SpaceBetween size="l">
                  <Alert type="info">
                    General portal settings will be available here in a future update.
                  </Alert>
                  <Box>
                    <Box variant="h3">Portal Information</Box>
                    <Box>Version: 1.0.0</Box>
                    <Box>Environment: Development</Box>
                  </Box>
                </SpaceBetween>
              ),
            },
          ]}
        />
      </Container>

      {/* Add Target Modal */}
      <Modal
        visible={showAddModal}
        onDismiss={() => setShowAddModal(false)}
        header="Add Compilation Target"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowAddModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSaveAdd}
                loading={saving}
                disabled={!targetName || !platform || !arch || !targetPlatform}
              >
                Add Target
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField label="Target Name" constraintText="Required">
            <Input
              value={targetName}
              onChange={({ detail }) => setTargetName(detail.value)}
              placeholder="e.g., Jetson Xavier"
            />
          </FormField>

          <FormField label="Platform" constraintText="Required">
            <Select
              selectedOption={platform}
              onChange={({ detail }) => setPlatform(detail.selectedOption)}
              options={platformOptions}
              placeholder="Select platform"
            />
          </FormField>

          <FormField label="Architecture" constraintText="Required">
            <Select
              selectedOption={arch}
              onChange={({ detail }) => setArch(detail.selectedOption)}
              options={archOptions}
              placeholder="Select architecture"
            />
          </FormField>

          <FormField label="SageMaker Neo Target Platform" constraintText="Required">
            <Input
              value={targetPlatform}
              onChange={({ detail }) => setTargetPlatform(detail.value)}
              placeholder="e.g., jetson_nano, ml_c5"
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Edit Target Modal */}
      <Modal
        visible={showEditModal}
        onDismiss={() => setShowEditModal(false)}
        header="Edit Compilation Target"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setShowEditModal(false)}>
                Cancel
              </Button>
              <Button
                variant="primary"
                onClick={handleSaveEdit}
                loading={saving}
                disabled={!targetName || !platform || !arch || !targetPlatform}
              >
                Save Changes
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <SpaceBetween size="m">
          <FormField label="Target Name" constraintText="Required">
            <Input
              value={targetName}
              onChange={({ detail }) => setTargetName(detail.value)}
            />
          </FormField>

          <FormField label="Platform" constraintText="Required">
            <Select
              selectedOption={platform}
              onChange={({ detail }) => setPlatform(detail.selectedOption)}
              options={platformOptions}
            />
          </FormField>

          <FormField label="Architecture" constraintText="Required">
            <Select
              selectedOption={arch}
              onChange={({ detail }) => setArch(detail.selectedOption)}
              options={archOptions}
            />
          </FormField>

          <FormField label="SageMaker Neo Target Platform" constraintText="Required">
            <Input
              value={targetPlatform}
              onChange={({ detail }) => setTargetPlatform(detail.value)}
            />
          </FormField>
        </SpaceBetween>
      </Modal>

      {/* Delete Confirmation Modal */}
      <ConfirmationModal
        visible={showDeleteModal}
        title="Delete Compilation Target"
        message="This action cannot be undone. Models will no longer be able to compile for this target."
        confirmButtonText="Delete"
        variant="danger"
        loading={saving}
        onConfirm={handleConfirmDelete}
        onCancel={() => setShowDeleteModal(false)}
      >
        <Box>
          Are you sure you want to delete{' '}
          <strong>{selectedItems.map((item) => item.name).join(', ')}</strong>?
        </Box>
      </ConfirmationModal>
    </>
  );
}
