#!/usr/bin/env node

/**
 * Generate frontend config.json from CDK outputs and branding configuration
 * 
 * Usage:
 *   node generate-frontend-config.js \
 *     --api-url https://api.example.com/v1 \
 *     --user-pool-id us-east-1_XXXXX \
 *     --user-pool-client-id xxxxx \
 *     --region us-east-1 \
 *     --branding-file ../config/branding.json \
 *     --output ../../frontend/public/config.json
 */

const fs = require('fs');
const path = require('path');

// Parse command line arguments
const args = process.argv.slice(2);
const getArg = (name) => {
  const index = args.indexOf(name);
  return index !== -1 ? args[index + 1] : null;
};

const apiUrl = getArg('--api-url');
const userPoolId = getArg('--user-pool-id');
const userPoolClientId = getArg('--user-pool-client-id');
const region = getArg('--region') || 'us-east-1';
const brandingFile = getArg('--branding-file') || path.join(__dirname, '../config/branding.json');
const outputFile = getArg('--output') || path.join(__dirname, '../../frontend/public/config.json');

// Validate required arguments
if (!apiUrl || !userPoolId || !userPoolClientId) {
  console.error('Error: Missing required arguments');
  console.error('Usage: node generate-frontend-config.js \\');
  console.error('  --api-url <url> \\');
  console.error('  --user-pool-id <id> \\');
  console.error('  --user-pool-client-id <id> \\');
  console.error('  --region <region> \\');
  console.error('  --branding-file <path> \\');
  console.error('  --output <path>');
  process.exit(1);
}

// Load branding configuration
let branding = {
  applicationName: 'Defect Detection Application Portal',
  companyName: 'AWS',
  logoUrl: '',
  faviconUrl: '',
  primaryColor: '#0073bb',
  supportEmail: 'support@example.com',
  documentationUrl: 'https://docs.example.com',
};

try {
  const brandingPath = path.resolve(__dirname, brandingFile);
  if (fs.existsSync(brandingPath)) {
    const brandingData = fs.readFileSync(brandingPath, 'utf8');
    branding = { ...branding, ...JSON.parse(brandingData) };
    console.log(`✓ Loaded branding from: ${brandingPath}`);
  } else {
    console.log(`⚠ Branding file not found: ${brandingPath}`);
    console.log('  Using default branding');
  }
} catch (error) {
  console.error(`⚠ Error loading branding file: ${error.message}`);
  console.log('  Using default branding');
}

// Generate config
const config = {
  apiUrl,
  userPoolId,
  userPoolClientId,
  region,
  branding,
};

// Write config file
try {
  const outputPath = path.resolve(__dirname, outputFile);
  const outputDir = path.dirname(outputPath);
  
  // Ensure output directory exists
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }
  
  fs.writeFileSync(outputPath, JSON.stringify(config, null, 2));
  console.log(`✓ Generated config.json: ${outputPath}`);
  console.log(`  Application: ${branding.applicationName}`);
  console.log(`  Company: ${branding.companyName}`);
  console.log(`  API URL: ${apiUrl}`);
} catch (error) {
  console.error(`✗ Error writing config file: ${error.message}`);
  process.exit(1);
}
