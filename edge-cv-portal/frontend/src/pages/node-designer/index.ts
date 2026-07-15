/**
 * Node_Designer pages (custom-node-designer).
 *
 * Directory structure shared by the Node_Designer views:
 *   - types.ts          wire shapes + declaration constants
 *   - api.ts            shared API client for the /plugins routes
 *   - badges.tsx        lifecycle / classification / build-status badges
 *   - declaration.ts    create-wizard form -> declaration assembly
 *   - zip.ts            scaffold zip download
 *   - PluginLibrary     Plugin_Record list (task 12.1)
 *   - PluginDetail      record detail with build log excerpts (task 12.1)
 *   - CreateWizard      declaration wizard -> scaffold -> build (task 12.1)
 *   - generate.ts       generate-panel chat state + error helpers (task 12.2)
 *   - GeneratePanel     prompt-based scaffold generation chat (task 12.2)
 *
 * Later tasks add the import views (12.3), the simulator view (12.4),
 * and the registration wizard / review queue (12.5) beside these.
 */
export { default as PluginLibrary } from './PluginLibrary';
export { default as PluginDetail } from './PluginDetail';
export { default as CreateWizard } from './CreateWizard';
export { default as GeneratePanel } from './GeneratePanel';
export { default as RegistrationWizard } from './RegistrationWizard';
export { default as ReviewQueue } from './ReviewQueue';
export { default as RegistrationPrompt } from './RegistrationPrompt';
export { default as SimulatorView } from './SimulatorView';
export { default as ImportView } from './ImportView';
export { nodeDesignerApi } from './api';
export * from './types';
