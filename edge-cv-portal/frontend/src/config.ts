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

export default config;
