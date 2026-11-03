/**
 * Suggested prompts per guide chapter (spec portal-ai-assistant Requirement
 * 3.6): at most four, chosen by the current route's chapter.
 */
import { chapterForRoute } from './routeChapters';

const GENERAL: string[] = [
  'What should I do next?',
  'Where am I on the setup path?',
  'What does this page do?',
];

const BY_CHAPTER: Record<string, string[]> = {
  '01-getting-started.md': ['What should I do first?', 'Where am I on the setup path?', 'How do I pick a use case?'],
  '02-use-cases.md': ['How do I create a use case?', 'Who can see this use case?', 'What does the S3 bucket setting do?'],
  '03-data-management.md': ['How do I upload images?', 'How many images does this use case have?', 'What folder layout should I use?'],
  '04-labeling.md': ['How do I start a labeling job?', 'Which labeling jobs are running?', 'What is model-assisted pre-labeling?'],
  '05-synthetic-data.md': ['When should I generate synthetic images?', 'What do the generation settings mean?'],
  '06-training.md': ['How do I train a model from my labels?', 'Which training jobs are running?', 'Why did my training fail?'],
  '07-models.md': ['Which models do I have?', 'How do I compile a model for a device?', 'What is the difference between compile and package?'],
  '08-workflows.md': ['How do I build a workflow?', 'Which nodes do I need for a camera and a model?', 'How do I validate and package a workflow?'],
  '09-workflow-tuning.md': ['What does anomaly tuning do?', 'How do I run a tuning session?'],
  '10-node-designer.md': ['What is a custom node?', 'Which plugins do I have?', 'How do I register a plugin as a node type?'],
  '11-components.md': ['Which components are deployed?', 'What is a Greengrass component?'],
  '12-builds.md': ['How do I build the LocalServer?', 'Which builds are running?', 'Why did my build fail?'],
  '13-deployments.md': ['Which deployments exist?', 'How do I deploy to a device?', 'What is in a deployment?'],
  '14-workflow-dashboards.md': ['How do I create a dashboard?', 'What telemetry is available?'],
  '15-devices.md': ['Which devices do I have?', 'How do I register a device?', 'Why is my device offline?'],
  '16-debug-log.md': ['How do I read the debug log?', 'What does this log entry mean?'],
  '17-plugin-review.md': ['What does the plugin review check?', 'Which plugins wait for review?'],
  '18-build-fleet.md': ['Which build servers exist?', 'How do I add a build server?'],
  '19-settings.md': ['What do these settings do?', 'How do I configure Bedrock?'],
  '20-user-manager.md': ['How do I add a user?', 'What can each role do?'],
  '21-audit-logs.md': ['What do the audit channels mean?', 'How do I find who changed a deployment?'],
};

export function suggestedPrompts(route: string): string[] {
  const chapter = chapterForRoute(route);
  const prompts = (chapter && BY_CHAPTER[chapter]) || GENERAL;
  return prompts.slice(0, 4);
}
