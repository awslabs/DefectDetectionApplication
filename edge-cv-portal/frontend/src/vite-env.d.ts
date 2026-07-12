/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_API_URL: string;
  readonly VITE_USER_POOL_ID: string;
  readonly VITE_USER_POOL_CLIENT_ID: string;
  readonly VITE_AWS_REGION: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}

// Build-time constants injected by vite.config.ts `define` for the version banner.
declare const __APP_VERSION__: string;
declare const __APP_GIT_SHA__: string;
declare const __APP_BUILD_TIME__: string;

