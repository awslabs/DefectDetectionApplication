/**
 * Workflow Manager frontend module: Workflow_Definition types, the node
 * catalog wire shapes, and the TypeScript mirrors of the workflow_core
 * port compatibility rules, parameter constraint predicate, and inline
 * validator checks (V4/V5).
 */

export * from './types';
export * from './cameraReference';
export * from './compatibility';
export * from './parameters';
export * from './inlineChecks';
export * from './metadataConfig';
export * from './builderGraph';
export * from './validationMarkers';
export * from './importAnalyzer';
export {
  default as WorkflowToolbar,
  canEditWorkflows,
  WORKFLOW_EDIT_ROLES,
  type WorkflowMeta,
  type WorkflowToolbarProps,
} from './WorkflowToolbar';
export {
  default as GenerateChatPanel,
  type ChatMessage,
  type GenerateChatPanelProps,
} from './GenerateChatPanel';
export {
  default as TestPanel,
  canTestWorkflows,
  isStubbedResult,
  SIMULATED_LIMITATION_TEXT,
  WORKFLOW_TEST_ROLES,
  type TestPanelProps,
} from './TestPanel';
