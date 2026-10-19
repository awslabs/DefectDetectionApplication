/**
 * Application configuration
 * This file should be generated during deployment with actual values
 */

export interface BrandingConfig {
  applicationName: string;
  companyName: string;
  logoUrl?: string;
  faviconUrl?: string;
  primaryColor?: string;
  supportEmail?: string;
  documentationUrl?: string;
}

export interface AppConfig {
  apiUrl: string;
  userPoolId: string;
  userPoolClientId: string;
  region: string;
  branding: BrandingConfig;
  /**
   * Mirror of the Portal handlers' PORTAL_REGISTRY_ENFORCED (a CDK context
   * value, default off). The frontend has no route that reports it, so the
   * deployed config.json carries it; absent means off, the same default the
   * backend applies. The workflow debug pages read it (workflow-debug-mode
   * design §7.2 "two switches"): while it is off they show the warning
   * "Registry enforcement is off" and leave the images control disabled,
   * which is what the API enforces with 409 registry_enforcement_required.
   * The server stays authoritative: a wrong value here only changes the
   * banner, never what the API accepts.
   */
  registryEnforced?: boolean;
}

// Default configuration for development
const defaultConfig: AppConfig = {
  apiUrl: import.meta.env.VITE_API_URL || 'http://localhost:3001/api/v1',
  userPoolId: import.meta.env.VITE_USER_POOL_ID || '',
  userPoolClientId: import.meta.env.VITE_USER_POOL_CLIENT_ID || '',
  region: import.meta.env.VITE_AWS_REGION || 'us-east-1',
  branding: {
    applicationName: 'Defect Detection Application Portal',
    companyName: 'AWS',
    supportEmail: 'support@example.com',
    documentationUrl: 'https://docs.example.com',
  },
};

// Try to load config from public/config.json (generated during deployment)
let config: AppConfig = defaultConfig;

export const loadConfig = async (): Promise<AppConfig> => {
  try {
    const response = await fetch('/config.json');
    if (response.ok) {
      const deployedConfig = await response.json();
      config = { ...defaultConfig, ...deployedConfig };
    }
  } catch (error) {
    console.warn('Could not load config.json, using default configuration');
  }
  return config;
};

export const getConfig = (): AppConfig => config;
/** True only when the deployed config says registry enforcement is on. */
export const isRegistryEnforced = (): boolean => config.registryEnforced === true;

/**
 * Build metadata baked in at build time (see vite.config.ts `define`).
 * Used by the bottom version banner so users can see exactly which portal
 * build is running. Wrapped in try/catch so a dev server without the defines
 * still renders sensible fallbacks.
 */
export interface BuildInfo {
  version: string;
  gitSha: string;
  buildTime: string;
}

export const getBuildInfo = (): BuildInfo => {
  const safe = (v: unknown, fallback: string) =>
    typeof v === 'string' && v.length > 0 ? v : fallback;
  let version = 'dev';
  let gitSha = 'unknown';
  let buildTime = '';
  try {
    version = safe(typeof __APP_VERSION__ !== 'undefined' ? __APP_VERSION__ : undefined, 'dev');
  } catch { /* not defined in this context */ }
  try {
    gitSha = safe(typeof __APP_GIT_SHA__ !== 'undefined' ? __APP_GIT_SHA__ : undefined, 'unknown');
  } catch { /* not defined */ }
  try {
    buildTime = safe(typeof __APP_BUILD_TIME__ !== 'undefined' ? __APP_BUILD_TIME__ : undefined, '');
  } catch { /* not defined */ }
  return { version, gitSha, buildTime };
};

export default config;
