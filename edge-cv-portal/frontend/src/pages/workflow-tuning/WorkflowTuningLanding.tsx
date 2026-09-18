/**
 * Workflow Tuning section landing page (quality-prompt-tuning, task 8.2,
 * Requirements 1.1, 1.2).
 *
 * The section is designed to hold further workflow-analysis tools; today it
 * lists its single tool, VLM/LLM Anomaly Tuning. `App.tsx` routes
 * `/workflow-tuning` here, behind the same workflow-edit roles that gate the
 * navigation entry.
 */
import { useNavigate } from 'react-router-dom';
import {
  Box,
  Button,
  Cards,
  ContentLayout,
  Header,
  Link,
  SpaceBetween,
} from '@cloudscape-design/components';

export interface WorkflowTuningTool {
  id: string;
  name: string;
  href: string;
  description: string;
}

/** The tools of the Workflow Tuning section, in display order. */
export const WORKFLOW_TUNING_TOOLS: readonly WorkflowTuningTool[] = [
  {
    id: 'anomaly',
    name: 'VLM/LLM Anomaly Tuning',
    href: '/workflow-tuning/anomaly',
    description:
      'Collect the images your anomaly-mode Bedrock and VLM inspection '
      + 'nodes really sent, label them, score candidate prompts against '
      + 'them, and apply the winner as a new workflow version.',
  },
];

export default function WorkflowTuningLanding() {
  const navigate = useNavigate();

  return (
    <ContentLayout
      header={
        <Header
          variant="h1"
          description="Analysis tools for deployed workflows"
        >
          Workflow Tuning
        </Header>
      }
    >
      <SpaceBetween size="l">
        <Cards<WorkflowTuningTool>
          data-testid="workflow-tuning-tools"
          cardDefinition={{
            header: (tool) => (
              <Link
                href={tool.href}
                onFollow={(event) => {
                  event.preventDefault();
                  navigate(tool.href);
                }}
              >
                {tool.name}
              </Link>
            ),
            sections: [
              {
                id: 'description',
                content: (tool) => <Box>{tool.description}</Box>,
              },
              {
                id: 'open',
                content: (tool) => (
                  <Button onClick={() => navigate(tool.href)}>
                    {`Open ${tool.name}`}
                  </Button>
                ),
              },
            ],
          }}
          cardsPerRow={[{ cards: 1 }, { minWidth: 600, cards: 2 }]}
          items={[...WORKFLOW_TUNING_TOOLS]}
          trackBy="id"
        />
      </SpaceBetween>
    </ContentLayout>
  );
}
