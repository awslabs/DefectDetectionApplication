import React from 'react';
import ReactDOM from 'react-dom/client';
import App from './App';
import { loadConfig, getConfig } from './config';

// Load configuration before rendering the app
loadConfig().then(() => {
  const config = getConfig();
  const branding = config.branding;

  // Update document title
  document.title = branding.applicationName;

  // Update favicon if provided
  if (branding.faviconUrl) {
    const favicon = document.getElementById('favicon') as HTMLLinkElement;
    if (favicon) {
      favicon.href = branding.faviconUrl;
    }
  }

  // Apply primary color if provided
  if (branding.primaryColor) {
    document.documentElement.style.setProperty('--primary-color', branding.primaryColor);
  }

  ReactDOM.createRoot(document.getElementById('root')!).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>
  );
});
