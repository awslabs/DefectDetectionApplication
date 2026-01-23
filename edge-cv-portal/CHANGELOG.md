# Changelog

All notable changes to the DDA Portal will be documented in this file.

## [Unreleased]

### Added - Data Accounts Management (2025-01-22)

#### Settings Page
- New Settings page for Portal Admins
- Data Accounts management UI (add, edit, delete, test connection)
- Connection testing to verify Data Account access
- Status indicators for registered Data Accounts

#### UseCase Onboarding Enhancement
- Data Account dropdown selector in UseCase onboarding wizard
- Auto-fill all fields when selecting from registered Data Accounts
- Three configuration methods: dropdown, file upload, or manual entry
- Smart behavior to prevent conflicts between methods

#### Simplified Data Account Setup
- Removed UseCase Account IDs requirement from Data Account deployment
- Portal automatically configures bucket policies during UseCase onboarding
- No redeployment needed when adding new UseCases
- Clear instructions in deployment script for portal registration

#### Backend
- New Lambda function: `data_accounts.py` with full CRUD operations
- 6 new API endpoints for Data Accounts management
- DynamoDB table: `dda-portal-data-accounts` with GSI on status
- Automatic bucket policy updates during UseCase creation

#### Documentation
- `DATA_ACCOUNTS_DEPLOYMENT.md` - Deployment guide
- `DATA_ACCOUNT_SIMPLIFIED_SETUP.md` - Simplified setup explanation
- `DATA_ACCOUNT_DROPDOWN_FEATURE.md` - Dropdown feature user guide
- `DEPLOYMENT_CHECKLIST.md` - Complete deployment checklist
- `IMPLEMENTATION_COMPLETE.md` - Implementation summary

#### Benefits
- **Faster UseCase Creation**: 5 seconds vs 2-3 minutes (dropdown vs manual)
- **No Manual Entry**: Select from dropdown instead of typing
- **Error-Free**: No typos or copy-paste mistakes
- **Scalable**: Register once, use for unlimited UseCases
- **Simplified Deployment**: No UseCase Account IDs needed upfront

### Changed
- Updated `deploy-account-role.sh` to remove UseCase Account IDs prompt for Data Account
- Updated `data-account-app.ts` to handle empty usecaseAccountIds array
- Updated `data-account-stack.ts` to support portal-managed bucket policies
- Enhanced deployment script with clear registration instructions

### Fixed
- TypeScript interface alignment between Settings and API responses
- Optional fields properly marked in DataAccount interface

---

## Previous Releases

### Initial Release
- Multi-tenant portal architecture
- UseCase Account management
- Data management and S3 integration
- Ground Truth labeling jobs
- SageMaker training integration
- Model compilation for edge devices
- Greengrass component management
- Device deployments and monitoring
- Audit logging
- Cross-account role management
