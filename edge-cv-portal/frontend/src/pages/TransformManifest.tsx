import { useSearchParams } from 'react-router-dom';
import { Container, Header, SpaceBetween, Alert } from '@cloudscape-design/components';
import ManifestTransformer from '../components/ManifestTransformer';

export default function TransformManifest() {
  const [searchParams] = useSearchParams();
  const usecaseId = searchParams.get('usecase_id');

  if (!usecaseId) {
    return (
      <Container>
        <Alert type="error" header="Missing Use Case">
          Please select a use case from the Labeling page.
        </Alert>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      <Container
        header={
          <Header
            variant="h1"
            description="Transform Ground Truth manifests to DDA-compatible format. This tool converts job-specific attribute names to standardized DDA attribute names required for training."
          >
            Manifest Transformer
          </Header>
        }
      >
        <SpaceBetween size="m">
          <Alert type="info" header="When to use this tool">
            Use this tool after completing a Ground Truth labeling job. Ground Truth creates manifests with job-specific attribute names that need to be transformed to DDA-compatible format before training.
          </Alert>
          
          <ManifestTransformer usecaseId={usecaseId} />
        </SpaceBetween>
      </Container>
    </SpaceBetween>
  );
}
