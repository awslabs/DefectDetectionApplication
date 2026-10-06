# Triage ledger summary

Scan `d5f01064-c0bb-4aac-91ff-5f55b65e2afd`, branch `remediation` at `4a3f960`, generated 2026-10-05T19:34:17+00:00.
441 findings, one entry each. Per-finding detail (paths, reasons, duplicates, cross-references, external inputs) is in `ledger.json`; findings are referenced by rule id and finding_id only.

Local scanner versions used for line resolution: Bandit 1.8.6, Checkov 3.2.255. Semgrep was not re-run.

Every `design_ref` names a `design.md` heading anchor, which check 5 of `check_ledger.py` confirms.

## Totals

| Disposition | Count |
|---|---|
| `REMEDIATE` | 30 |
| `FALSE_POSITIVE` | 411 |

| FALSE_POSITIVE sub_reason | Count |
|---|---|
| `open-source-constraint` | 15 |
| `scanner-misread` | 79 |
| `test-only` | 302 |
| `vendored-or-untracked` | 10 |
| `already-mitigated` | 5 |

## By rule

| Scanner | Rule | Total | REMEDIATE | FALSE_POSITIVE | IGNORED_IMPAIRS_FUNCTION | FP sub_reasons |
|---|---|---|---|---|---|---|
| Scanner-X | `scanner-x/docker-image-source` | 15 | 0 | 15 | 0 | open-source-constraint 15 |
| Scanner-X | `scanner-x/plaintext-http` | 2 | 0 | 2 | 0 | scanner-misread 2 |
| Scanner-X | `scanner-x/sns-topic-encryption` | 2 | 1 | 1 | 0 | test-only 1 |
| BANDIT | `B102` | 13 | 0 | 13 | 0 | test-only 13 |
| BANDIT | `B104` | 4 | 0 | 4 | 0 | scanner-misread 4 |
| BANDIT | `B105` | 252 | 0 | 252 | 0 | scanner-misread 56, test-only 196 |
| BANDIT | `B106` | 11 | 0 | 11 | 0 | scanner-misread 6, test-only 5 |
| BANDIT | `B107` | 1 | 0 | 1 | 0 | scanner-misread 1 |
| BANDIT | `B301` | 5 | 2 | 3 | 0 | test-only 3 |
| BANDIT | `B307` | 3 | 0 | 3 | 0 | vendored-or-untracked 3 |
| BANDIT | `B310` | 10 | 2 | 8 | 0 | test-only 2, vendored-or-untracked 2, already-mitigated 4 |
| BANDIT | `B324` | 5 | 2 | 3 | 0 | test-only 3 |
| BANDIT | `B403` | 7 | 2 | 5 | 0 | scanner-misread 1, test-only 4 |
| BANDIT | `B604` | 1 | 0 | 1 | 0 | scanner-misread 1 |
| BANDIT | `B608` | 6 | 0 | 6 | 0 | scanner-misread 1, test-only 5 |
| CHECKOV | `CKV_AWS_107` | 1 | 0 | 1 | 0 | test-only 1 |
| CHECKOV | `CKV_AWS_108` | 1 | 0 | 1 | 0 | test-only 1 |
| CHECKOV | `CKV_AWS_109` | 2 | 1 | 1 | 0 | test-only 1 |
| CHECKOV | `CKV_AWS_111` | 9 | 4 | 5 | 0 | test-only 5 |
| CHECKOV | `CKV_AWS_119` | 4 | 2 | 2 | 0 | test-only 2 |
| CHECKOV | `CKV_AWS_26` | 2 | 1 | 1 | 0 | test-only 1 |
| CHECKOV | `CKV_AWS_27` | 10 | 6 | 4 | 0 | test-only 4 |
| Semgrep OSS | `generic.secrets.security.detected-aws-access-key-id-value` | 2 | 0 | 2 | 0 | test-only 2 |
| Semgrep OSS | `generic.secrets.security.detected-jwt-token` | 1 | 0 | 1 | 0 | test-only 1 |
| Semgrep OSS | `python.jwt.security.jwt-python-hardcoded-secret` | 1 | 0 | 1 | 0 | test-only 1 |
| Semgrep OSS | `python.jwt.security.unverified-jwt-decode` | 1 | 1 | 0 | 0 | - |
| Semgrep OSS | `python.lang.security.audit.dangerous-subprocess-use-audit` | 62 | 3 | 59 | 0 | scanner-misread 5, test-only 48, vendored-or-untracked 5, already-mitigated 1 |
| Semgrep OSS | `python.lang.security.audit.dangerous-subprocess-use-tainted-env-args` | 1 | 1 | 0 | 0 | - |
| Semgrep OSS | `python.lang.security.deserialization.avoid-dill` | 2 | 2 | 0 | 0 | - |
| Semgrep OSS | `python.sqlalchemy.security.sqlalchemy-execute-raw-query` | 5 | 0 | 5 | 0 | scanner-misread 2, test-only 3 |

### `scanner-x/docker-image-source` (Scanner-X, 15)

FALSE_POSITIVE / `open-source-constraint` (15): `edge-cv-portal/plugin-build-images/Dockerfile.arm64_jp5`, `edge-cv-portal/plugin-build-images/Dockerfile.arm64_jp6`, `edge-cv-portal/plugin-build-images/Dockerfile.arm64_jp7`, `edge-cv-portal/plugin-build-images/Dockerfile.x86_64_nvidia`, `src/backend/Dockerfile.jp5`, `src/backend/Dockerfile.jp6` (6), `src/backend/Dockerfile.jp7`, `src/edgemlsdk/Dockerfile.jp5`, `src/edgemlsdk/Dockerfile.jp6`, `src/edgemlsdk/Dockerfile.jp7`

### `scanner-x/plaintext-http` (Scanner-X, 2)

FALSE_POSITIVE / `scanner-misread` (2): `hmi/index.html`, `hmi/triple.html`

### `scanner-x/sns-topic-encryption` (Scanner-X, 2)

REMEDIATE:
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-0` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:43560` (task 6) -> `design.md#r14-sns-encryption`

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json`

### `B102` (BANDIT, 13)

FALSE_POSITIVE / `test-only` (13): `edge-cv-portal/backend/layers/workflow_core/tests/test_catalog_content.py`, `edge-cv-portal/backend/layers/workflow_core/tests/test_catalog_custom_python_source.py`, `test/backend-test/deploy_reliability/test_shutdown_handler_exploration.py`, `test/backend-test/deploy_reliability/test_shutdown_handler_preservation.py`, `test/backend-test/gstreamer/test_property_appsrc_frame_stride_preservation.py` (2), `test/backend-test/workflow_engine/test_dda_frames_http_timeout.py`, `test/backend-test/workflow_engine/test_overlay_image_serving.py`, `test/backend-test/workflow_engine/test_property_dda_frames_fetch_failures.py`, `test/backend-test/workflow_engine/test_property_dda_frames_http_fetch.py`, `test/backend-test/workflow_engine/test_property_dda_frames_prefix_gate.py`, `test/backend-test/workflow_engine/test_property_dda_frames_preservation.py`, `test/backend-test/workflow_engine/test_property_frame_helpers.py`

### `B104` (BANDIT, 4)

FALSE_POSITIVE / `scanner-misread` (4): `edge-cv-portal/backend/tests/test_camera_binding_submission.py`, `test/backend-test/workflow_engine/test_static_camera_binding_preservation.py` (3)

### `B105` (BANDIT, 252)

FALSE_POSITIVE / `scanner-misread` (56): `edge-cv-portal/backend/functions/build_reconciliation.py`, `edge-cv-portal/backend/functions/data_accounts.py`, `edge-cv-portal/backend/functions/datasets.py`, `edge-cv-portal/backend/functions/dda_autolabel_worker.py`, `edge-cv-portal/backend/functions/dda_labeling.py`, `edge-cv-portal/backend/functions/stream_credentials.py`, `edge-cv-portal/backend/functions/synthetic_core.py` (2), `edge-cv-portal/backend/functions/token_service.py`, `edge-cv-portal/backend/functions/user_admin.py`, `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/anomaly_invocation.py`, `edge-cv-portal/backend/layers/workflow_core/python/workflow_core/stream_url.py` (3), `edge-cv-portal/backend/layers/workflow_core/tests/test_property_stream_url_rules.py`, `edge-cv-portal/backend/tests/test_bedrock_configuration.py`, `edge-cv-portal/backend/tests/test_bedrock_model_options_image_limit.py`, `edge-cv-portal/backend/tests/test_dda_labeling_preview_routes.py`, `edge-cv-portal/backend/tests/test_deployment_preflight_properties.py` (2), `edge-cv-portal/backend/tests/test_llm_sizing_integration.py`, `edge-cv-portal/backend/tests/test_model_token_limits_settings.py`, `edge-cv-portal/backend/tests/test_property_anomaly_invocation.py`, `edge-cv-portal/backend/tests/test_property_authenticated_truncated_status.py`, `edge-cv-portal/backend/tests/test_property_bedrock_global_config_preservation.py`, `edge-cv-portal/backend/tests/test_property_bedrock_model_options_additive.py`, `edge-cv-portal/backend/tests/test_property_exactly_once_exchange.py`, `edge-cv-portal/backend/tests/test_property_gsam_preview_routes.py`, `edge-cv-portal/backend/tests/test_property_model_token_limits_isolation.py`, `edge-cv-portal/backend/tests/test_property_sizing_validation_guards.py`, `edge-cv-portal/backend/tests/test_property_token_budget_plumbing.py`, `edge-cv-portal/backend/tests/test_quick_setup_arch_recording.py`, `src/backend/local_auth/session_tokens.py`, `src/backend/vllm_runtime/manager.py` (5), `src/backend/workflow_engine/pipeline_executor.py`, `src/backend/workflow_engine/vendor/workflow_core/anomaly_invocation.py`, `src/backend/workflow_engine/vendor/workflow_core/stream_url.py` (3), `src/edgemlsdk/src/test/longevity/deploy.py`, `test/backend-test/backend_jammy_pkgs/_jammy_support.py`, `test/backend-test/edgemlsdk_pythondev/_pythondev_support.py`, `test/backend-test/edgemlsdk_pythondev/test_bug_condition_exploration.py`, `test/backend-test/portal_builds/test_bootstrap_zip_preservation.py`, `test/backend-test/security/preservation/test_preservation_iam_readme_prose.py`, `test/backend-test/security/preservation/test_preservation_secrets_deploy.py`, `test/backend-test/security/test_secrets_bug_condition_exploration.py`, `test/backend-test/stream_ingest/test_snapshot_excludes_credentials.py`, `test/backend-test/stream_ingest/test_stream_settings_migration.py`, `test/backend-test/vllm_latency/test_property_1_breakdown_wellformedness.py` (2), `test/backend-test/workflow_engine/test_continuous_state_migration.py`

FALSE_POSITIVE / `test-only` (196): `edge-cv-portal/backend/layers/workflow_core/tests/test_validator_stream_and_analytics.py`, `edge-cv-portal/backend/tests/test_camera_registry_stream_credentials.py` (5), `edge-cv-portal/backend/tests/test_git_sync.py`, `edge-cv-portal/backend/tests/test_git_sync_runner.py`, `edge-cv-portal/backend/tests/test_plugin_fetch_runner.py`, `edge-cv-portal/backend/tests/test_private_repo_import.py`, `edge-cv-portal/backend/tests/test_property_import_source.py`, `edge-cv-portal/backend/tests/test_property_stream_binding_compatibility.py`, `edge-cv-portal/backend/tests/test_stream_binding_deployment.py` (2), `edge-cv-portal/backend/tests/test_user_admin_scaffold.py` (2), `edge-cv-portal/backend/tests/test_user_admin_set_password.py`, `test/backend-test/camera_sync/test_stream_camera_sync_agent.py`, `test/backend-test/local_auth/test_local_auth_endpoints.py` (3), `test/backend-test/portal_builds/test_bootstrap_gate_property.py` (3), `test/backend-test/portal_builds/test_bootstrap_zip_exploration.py` (3), `test/backend-test/portal_builds/test_bootstrap_zip_preservation.py` (3), `test/backend-test/portal_builds/test_branch_discovery_authorization.py` (3), `test/backend-test/portal_builds/test_build_authorization_preservation.py` (3), `test/backend-test/portal_builds/test_build_config_rbac_and_audit.py` (3), `test/backend-test/portal_builds/test_build_diagnostic_api.py` (3), `test/backend-test/portal_builds/test_build_events_idempotence_and_audit.py` (3), `test/backend-test/portal_builds/test_build_fleet_lifecycle_and_audit.py` (3), `test/backend-test/portal_builds/test_build_history_ordering.py` (3), `test/backend-test/portal_builds/test_build_jobs_rbac_audit.py` (3), `test/backend-test/portal_builds/test_build_reconciliation_properties.py` (3), `test/backend-test/portal_builds/test_build_reconciliation_unit.py`, `test/backend-test/portal_builds/test_command_event_reconciliation.py` (3), `test/backend-test/portal_builds/test_default_repository_config.py` (3), `test/backend-test/portal_builds/test_dispatcher_command_reconciliation.py` (3), `test/backend-test/portal_builds/test_dispatcher_tick_integration.py` (3), `test/backend-test/portal_builds/test_execution_failure_exploration.py` (3), `test/backend-test/portal_builds/test_execution_failure_preservation.py` (3), `test/backend-test/portal_builds/test_exit75_deferral_exploration.py` (3), `test/backend-test/portal_builds/test_exit75_deferral_preservation.py` (3), `test/backend-test/portal_builds/test_exit75_deferral_regressions.py` (3), `test/backend-test/portal_builds/test_jp7_capability_gate_unit.py` (3), `test/backend-test/portal_builds/test_jp7_dedicated_capability_properties.py` (3), `test/backend-test/portal_builds/test_jp7_dedicated_flow_integration.py` (3), `test/backend-test/portal_builds/test_jp7_dispatcher_tick_integration.py` (3), `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_exploration.py` (3), `test/backend-test/portal_builds/test_jp7_ephemeral_provisioning_properties.py` (3), `test/backend-test/portal_builds/test_jp7_mixed_batch_tick_integration.py` (3), `test/backend-test/portal_builds/test_jp7_resolve_ami_unit.py` (3), `test/backend-test/portal_builds/test_jp7_unmapped_pairing_tick_integration.py` (3), `test/backend-test/portal_builds/test_jwt_admin_build_submit_authorization.py` (3), `test/backend-test/portal_builds/test_no_live_validation_contract.py` (4), `test/backend-test/portal_builds/test_persistence_iff_accept.py` (3), `test/backend-test/portal_builds/test_preflight_agent_contract.py` (3), `test/backend-test/portal_builds/test_preflight_target_matrix_properties.py` (4), `test/backend-test/portal_builds/test_ref_aware_bootstrap_property.py`, `test/backend-test/portal_builds/test_region_export_unit.py`, `test/backend-test/portal_builds/test_run_as_ubuntu_unit.py`, `test/backend-test/portal_builds/test_source_dir_alignment_property.py` (3), `test/backend-test/portal_builds/test_source_selection_exploration.py` (3), `test/backend-test/portal_builds/test_source_selection_preservation.py` (3), `test/backend-test/portal_builds/test_source_selection_snapshot_property.py` (3), `test/backend-test/portal_builds/test_storage_exhaustion_exploration.py` (3), `test/backend-test/portal_builds/test_terminal_effects_ledger.py` (3), `test/backend-test/portal_builds/test_terminal_effects_properties.py` (3), `test/backend-test/portal_builds/test_ubuntu_flavor_config_unit.py` (3), `test/backend-test/portal_builds/test_ubuntu_flavor_fleet_view_properties.py` (3), `test/backend-test/portal_builds/test_ubuntu_flavor_launch_properties.py` (3), `test/backend-test/portal_builds/test_ubuntu_pro_ami_tables_unit.py` (3), `test/backend-test/portal_builds/test_ubuntu_pro_resolver_properties.py` (3), `test/backend-test/security/preservation/_s3_preservation_support.py` (2), `test/backend-test/security/preservation/test_preservation_deploy_ssm.py` (2), `test/backend-test/security/preservation/test_preservation_secrets_deploy.py` (2), `test/backend-test/security/preservation/test_preservation_secrets_jwt.py` (2), `test/backend-test/security/test_secrets_bug_condition_exploration.py` (3), `test/backend-test/stream_ingest/integration/test_stream_worker_gstreamer.py`, `test/backend-test/stream_ingest/test_credential_store.py` (3), `test/backend-test/stream_ingest/test_log_redaction.py` (2), `test/backend-test/stream_ingest/test_snapshot_excludes_credentials.py`, `test/backend-test/stream_ingest/test_stream_image_source_api.py`, `test/backend-test/stream_ingest/test_stream_ingest_units.py`, `test/backend-test/stream_ingest/test_stream_source_domains.py`, `test/on-hardware/harness/selftest/fake_device.py`, `test/on-hardware/harness/selftest/test_client.py` (4), `test/on-hardware/harness/selftest/test_config.py`, `test/on-hardware/harness/selftest/test_e2e_fake_device.py`

### `B106` (BANDIT, 11)

FALSE_POSITIVE / `scanner-misread` (6): `test/backend-test/jp6_vllm_kv_cache_oom/test_property_failure_classification.py` (3), `test/backend-test/security/dependency_audit.py` (3)

FALSE_POSITIVE / `test-only` (5): `edge-cv-portal/backend/tests/test_plugin_fetch_runner.py`, `edge-cv-portal/backend/tests/test_quick_setup_arch_recording.py`, `edge-cv-portal/test-sandbox/tests/integration/conftest.py`, `test/backend-test/security/test_bug_condition_exploration.py`, `test/backend-test/security/test_secrets_bug_condition_exploration.py`

### `B107` (BANDIT, 1)

FALSE_POSITIVE / `scanner-misread` (1): `test/backend-test/security/preservation/_iam_preservation_support.py`

### `B301` (BANDIT, 5)

REMEDIATE:
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-0` `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` (task 4) -> `design.md#r10-deserialization`
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-3` `src/backend/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` (task 4) -> `design.md#r10-deserialization`

FALSE_POSITIVE / `test-only` (3): `test/backend-test/security/preservation/test_preservation_deserialization_roundtrip.py` (3)

### `B307` (BANDIT, 3)

FALSE_POSITIVE / `vendored-or-untracked` (3): `edge-cv-portal/backend/layers/workflow_core/python/attr/_make.py`, `edge-cv-portal/backend/layers/workflow_core/python/typing_extensions.py` (2)

### `B310` (BANDIT, 10)

REMEDIATE:
- `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0` `src/backend/workflow_engine/payload_fetch.py:301` (task 3) -> `design.md#r9-url-fetching`
- `95141da5-9489-42a9-a8d5-e066182a8f68-0` `test/on-hardware/register_vllm_models.py:128` (task 3) -> `design.md#r9-url-fetching`

FALSE_POSITIVE / `test-only` (2): `edge-cv-portal/backend/tests/test_gsam_mask_offset_exploration.py`, `edge-cv-portal/backend/tests/test_gsam_mask_offset_preservation.py`

FALSE_POSITIVE / `vendored-or-untracked` (2): `edge-cv-portal/backend/layers/workflow_core/python/jsonschema/validators.py` (2)

FALSE_POSITIVE / `already-mitigated` (4): `edge-cv-portal/backend/functions/build_source.py`, `edge-cv-portal/backend/functions/vllm_fit_check.py`, `edge-cv-portal/backend/grounded-sam-worker/handler.py`, `edge-cv-portal/backend/sam-worker/handler.py`

### `B324` (BANDIT, 5)

REMEDIATE:
- `93e57454-5aca-4e33-a9a0-fe6de96905c1-0` `src/backend/camera_discovery/discovery.py:170` (task 5) -> `design.md#r12-identifier-hashes`
- `e1583a8e-82f5-468a-829e-9cc91e0e5bb7-0` `src/backend/camera_discovery/aravis.py:105` (task 5) -> `design.md#r12-identifier-hashes`

FALSE_POSITIVE / `test-only` (3): `test/backend-test/camera_discovery/test_aravis_enumeration.py`, `test/backend-test/camera_discovery/test_camera_enumeration.py`, `test/backend-test/camera_sync/test_property_static_camera_dedup_preservation.py`

### `B403` (BANDIT, 7)

REMEDIATE:
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-4` `src/backend/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:60` (task 4) -> `design.md#r10-deserialization`
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-1` `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:60` (task 4) -> `design.md#r10-deserialization`

FALSE_POSITIVE / `scanner-misread` (1): `edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py`

FALSE_POSITIVE / `test-only` (4): `test/backend-test/security/preservation/test_preservation_deserialization_roundtrip.py` (2), `test/backend-test/security/test_bug_condition_exploration.py` (2)

### `B604` (BANDIT, 1)

FALSE_POSITIVE / `scanner-misread` (1): `edge-cv-portal/backend/tests/test_camera_sync_stream_capabilities.py`

### `B608` (BANDIT, 6)

FALSE_POSITIVE / `scanner-misread` (1): `edge-cv-portal/backend/tests/test_git_sync_runner.py`

FALSE_POSITIVE / `test-only` (5): `test/backend-test/camera_sync/test_no_migration_smoke.py`, `test/backend-test/stream_ingest/test_stream_settings_migration.py` (2), `test/backend-test/workflow_engine/test_continuous_state_migration.py`, `test/backend-test/workflow_engine/test_workflow_migration_safety.py`

### `CKV_AWS_107` (CHECKOV, 1)

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json`

### `CKV_AWS_108` (CHECKOV, 1)

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json`

### `CKV_AWS_109` (CHECKOV, 2)

REMEDIATE:
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-1` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:1717` (task 9) -> `design.md#r15-least-privilege-iam`

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json`

### `CKV_AWS_111` (CHECKOV, 9)

REMEDIATE:
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-4` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:46006` (task 9) -> `design.md#r15-least-privilege-iam`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-2` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:1717` (task 9) -> `design.md#r15-least-privilege-iam`
- `ec660377-3ea5-4738-962a-1e6d384643fa-0` `test/backend-test/security/baselines/iam_baseline_DDAPortalUseCaseAccountStack.template.json:110` (task 9) -> `design.md#r15-least-privilege-iam`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-3` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:3903` (task 9) -> `design.md#r15-least-privilege-iam`

FALSE_POSITIVE / `test-only` (5): `test/backend-test/security/baselines/iam_baseline_DDAPortalUseCaseAccountStack.unfixed.template.json`, `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json` (4)

### `CKV_AWS_119` (CHECKOV, 4)

REMEDIATE:
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-6` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:31570` (task 8) -> `design.md#r14-dynamodb-cmk`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-5` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:31543` (task 8) -> `design.md#r14-dynamodb-cmk`

FALSE_POSITIVE / `test-only` (2): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json` (2)

### `CKV_AWS_26` (CHECKOV, 2)

REMEDIATE:
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-7` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:43560` (task 6) -> `design.md#r14-sns-encryption`

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json`

### `CKV_AWS_27` (CHECKOV, 10)

REMEDIATE:
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-11` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:34238` (task 7) -> `design.md#r14-sqs-encryption`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-8` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:29180` (task 7) -> `design.md#r14-sqs-encryption`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-10` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:31645` (task 7) -> `design.md#r14-sqs-encryption`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-13` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:29132` (task 7) -> `design.md#r14-sqs-encryption`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-9` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:31597` (task 7) -> `design.md#r14-sqs-encryption`
- `bcba7f05-bd61-4f0d-a0ac-672b1d990f2c-12` `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.template.json:34286` (task 7) -> `design.md#r14-sqs-encryption`

FALSE_POSITIVE / `test-only` (4): `test/backend-test/security/baselines/iam_baseline_EdgeCVPortalComputeStack.unfixed.template.json` (4)

### `generic.secrets.security.detected-aws-access-key-id-value` (Semgrep OSS, 2)

FALSE_POSITIVE / `test-only` (2): `test/backend-test/portal_builds/test_no_live_validation_contract.py`, `test/backend-test/portal_builds/test_preflight_target_matrix_properties.py`

### `generic.secrets.security.detected-jwt-token` (Semgrep OSS, 1)

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/preservation/test_preservation_secrets_jwt.py`

### `python.jwt.security.jwt-python-hardcoded-secret` (Semgrep OSS, 1)

FALSE_POSITIVE / `test-only` (1): `test/backend-test/security/preservation/test_preservation_secrets_jwt.py`

### `python.jwt.security.unverified-jwt-decode` (Semgrep OSS, 1)

REMEDIATE:
- `490a14e8-75e3-48e4-af23-ae95676b238c-0` `edge-cv-portal/backend/functions/jwt_authorizer.py:153` (task 1) -> `design.md#r6-jwt-authorizer`

### `python.lang.security.audit.dangerous-subprocess-use-audit` (Semgrep OSS, 62)

REMEDIATE:
- `feb1c815-fb14-44af-b148-6502a6f1fad8-0` `datasets/detection_training/export_checkpoint.py:445` (task 2) -> `design.md#r7-process-execution`
- `69ef51f8-d65b-4291-a4ae-d447bbd2367d-0` `src/backend/workflow_engine/python_bridge.py:1043` (task 2) -> `design.md#r7-process-execution`
- `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0` `src/backend/utils/utils.py:157` (task 2) -> `design.md#r7-process-execution`

FALSE_POSITIVE / `scanner-misread` (5): `datasets/sync_captures_to_s3.py`, `edge-cv-portal/backend/functions/camera_registry.py`, `src/backend/stream_ingest/capabilities.py` (2), `src/backend/stream_ingest/launch.py`

FALSE_POSITIVE / `test-only` (48): `edge-cv-portal/backend/tests/test_dda_llm_image.py`, `edge-cv-portal/backend/tests/test_git_sync_runner.py`, `edge-cv-portal/backend/tests/test_plugin_fetch_runner.py` (2), `edge-cv-portal/backend/tests/test_property_image_downscaler.py`, `edge-cv-portal/test-sandbox/tests/integration/conftest.py` (2), `edge-cv-portal/test-sandbox/tests/integration/test_simulate_e2e.py`, `test/backend-test/build_script/test_build_only_flag_property.py`, `test/backend-test/build_server_disk_pruning/test_exploration_disk_pruning.py`, `test/backend-test/camera_shadow_sync/test_gap1_exploration.py` (2), `test/backend-test/camera_shadow_sync/test_iot_policy_statement_properties.py`, `test/backend-test/csi_nvargus_optional/test_capture_supervisor_behaviors.py` (2), `test/backend-test/csi_nvargus_optional/test_property_csi_capture_supervisor.py`, `test/backend-test/csi_nvargus_optional/test_property_csi_preservation.py`, `test/backend-test/dda_triton/test_triton_inference_runtimes_bug.py`, `test/backend-test/deploy_reliability/test_defect_e_preservation.py`, `test/backend-test/host_scripts/test_docker_profile_selection.py`, `test/backend-test/jp6_vllm_kv_cache_oom/test_integration_preflight_prep.py` (2), `test/backend-test/portal_builds/test_agent_tail_truncation_properties.py`, `test/backend-test/portal_builds/test_cli_launcher_flavor.py`, `test/backend-test/portal_builds/test_ref_aware_bootstrap_property.py`, `test/backend-test/portal_builds/test_run_as_ubuntu_unit.py` (2), `test/backend-test/portal_builds/test_source_selection_exploration.py` (2), `test/backend-test/portal_builds/test_source_selection_preservation.py`, `test/backend-test/portal_builds/test_source_sync_commands_unit.py`, `test/backend-test/portal_builds/test_storage_exhaustion_exploration.py`, `test/backend-test/security/preservation/test_preservation_docker_profile.py` (2), `test/backend-test/security/preservation/test_preservation_iam_cdk_synth.py`, `test/backend-test/security/repo_audit.py`, `test/backend-test/security/secrets_audit.py`, `test/backend-test/security/test_s3_squat_bug_condition_exploration.py` (2), `test/backend-test/static_video_camera/video_clip_library.py`, `test/backend-test/stream_ingest/integration/rtmp_test_server.py`, `test/backend-test/stream_ingest/integration/rtsp_test_responder.py`, `test/backend-test/stream_ingest/test_image_stream_components.py`, `test/backend-test/stream_ingest/test_snapshot_excludes_credentials.py`, `test/backend-test/stream_ingest/test_stream_settings_migration.py`, `test/backend-test/workflow_engine/test_continuous_state_migration.py`, `test/backend-test/workflow_engine/test_workflow_migration_safety.py` (2)

FALSE_POSITIVE / `vendored-or-untracked` (5): `edge-cv-portal/backend/layers/workflow_core/python/jsonschema/benchmarks/import_benchmark.py`, `edge-cv-portal/backend/layers/workflow_core/python/jsonschema/tests/test_cli.py` (3), `edge-cv-portal/backend/layers/workflow_core/python/jsonschema/tests/test_deprecations.py`

FALSE_POSITIVE / `already-mitigated` (1): `datasets/detection_training/_common.py`

### `python.lang.security.audit.dangerous-subprocess-use-tainted-env-args` (Semgrep OSS, 1)

REMEDIATE:
- `feb1c815-fb14-44af-b148-6502a6f1fad8-1` `datasets/detection_training/export_checkpoint.py:446` (task 2) -> `design.md#r7-process-execution`

### `python.lang.security.deserialization.avoid-dill` (Semgrep OSS, 2)

REMEDIATE:
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-2` `edge-cv-portal/test-sandbox/dda_triton_resources/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` (task 4) -> `design.md#r10-deserialization`
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-5` `src/backend/lyra_science_processing_utils/model_processors/reference_image_map_migration.py:68` (task 4) -> `design.md#r10-deserialization`

### `python.sqlalchemy.security.sqlalchemy-execute-raw-query` (Semgrep OSS, 5)

FALSE_POSITIVE / `scanner-misread` (2): `test/backend-test/workflow_engine/test_property_python_source_metadata_merge.py`, `test/backend-test/workflow_engine/test_property_trigger_seeding.py`

FALSE_POSITIVE / `test-only` (3): `test/backend-test/camera_sync/test_no_migration_smoke.py`, `test/backend-test/workflow_engine/test_workflow_migration_safety.py` (2)

## Deliberate behavior changes

Behavior a remediation task changes on purpose (Requirement 16.6), from each entry's `deliberate_changes` in `ledger.json`:

- `f035ce5a-9b67-4aac-9ec0-b5f597f0fdcc-0` (`B310`, task 3):
  - URLs in payload reference errors and in the run log's Payload_Reference line are redacted to scheme://host[:port]/path: user information and the fragment are dropped, a query becomes ?<redacted>, and exception text appended to a fetch error has those parts removed.
  - A redirect is followed only to http or https, inside the node's allowed_uri_prefixes (an empty list still admits any remote source), without an https to http downgrade and without user information. Any other redirect, for every status the runtime follows (301, 302, 303, 307, and 308 on Python 3.11), fails the node's Payload_Reference with an error naming the rejected scheme or the redacted destination, and its target isn't requested.
  - An http(s) Payload_Reference URL with user information is refused before any request (URLs with embedded credentials are not supported). Such URLs never fetched, because urllib doesn't send URL user information as credentials.
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-0` (`B301`, task 4):
  - A legacy reference-image map is loaded only through open_trusted_legacy_map: a file that resolves outside TRUSTED_LEGACY_MAP_ROOTS (/aws_dda/greengrass/v2/packages/artifacts-unarchived and /aws_dda/dda_triton/triton_model_repo), isn't a regular file, is owned by a uid other than root or the effective uid, or is writable by group or others is refused with UntrustedLegacyMapError before the deserializer is imported, and nothing is loaded or written. A group-writable map in the Triton model repository, on a station where the installer's 770 pass covered /aws_dda/dda_triton, is refused with the fix (chown root, chmod go-w) in the message; an unarchived artifact Greengrass gave to the component's run user is refused with Owned by uid N (open decision 13).
  - The migration CLI reports a refusal on stderr, naming the path and the failed condition, and exits with code 2; programmatic callers get the exception. Its outputs are unchanged: only the JSON and allow_pickle=False NumPy sidecars, with --reference-image-map-file still choosing the output base.
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-3` (`B301`, task 4):
  - A legacy reference-image map is loaded only through open_trusted_legacy_map: a file that resolves outside TRUSTED_LEGACY_MAP_ROOTS (/aws_dda/greengrass/v2/packages/artifacts-unarchived and /aws_dda/dda_triton/triton_model_repo), isn't a regular file, is owned by a uid other than root or the effective uid, or is writable by group or others is refused with UntrustedLegacyMapError before the deserializer is imported, and nothing is loaded or written. A group-writable map in the Triton model repository, on a station where the installer's 770 pass covered /aws_dda/dda_triton, is refused with the fix (chown root, chmod go-w) in the message; an unarchived artifact Greengrass gave to the component's run user is refused with Owned by uid N (open decision 13).
  - The migration CLI reports a refusal on stderr, naming the path and the failed condition, and exits with code 2; programmatic callers get the exception. Its outputs are unchanged: only the JSON and allow_pickle=False NumPy sidecars, with --reference-image-map-file still choosing the output base.
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-4` (`B403`, task 4):
  - A legacy reference-image map is loaded only through open_trusted_legacy_map: a file that resolves outside TRUSTED_LEGACY_MAP_ROOTS (/aws_dda/greengrass/v2/packages/artifacts-unarchived and /aws_dda/dda_triton/triton_model_repo), isn't a regular file, is owned by a uid other than root or the effective uid, or is writable by group or others is refused with UntrustedLegacyMapError before the deserializer is imported, and nothing is loaded or written. A group-writable map in the Triton model repository, on a station where the installer's 770 pass covered /aws_dda/dda_triton, is refused with the fix (chown root, chmod go-w) in the message; an unarchived artifact Greengrass gave to the component's run user is refused with Owned by uid N (open decision 13).
  - The migration CLI reports a refusal on stderr, naming the path and the failed condition, and exits with code 2; programmatic callers get the exception. Its outputs are unchanged: only the JSON and allow_pickle=False NumPy sidecars, with --reference-image-map-file still choosing the output base.
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-1` (`B403`, task 4):
  - A legacy reference-image map is loaded only through open_trusted_legacy_map: a file that resolves outside TRUSTED_LEGACY_MAP_ROOTS (/aws_dda/greengrass/v2/packages/artifacts-unarchived and /aws_dda/dda_triton/triton_model_repo), isn't a regular file, is owned by a uid other than root or the effective uid, or is writable by group or others is refused with UntrustedLegacyMapError before the deserializer is imported, and nothing is loaded or written. A group-writable map in the Triton model repository, on a station where the installer's 770 pass covered /aws_dda/dda_triton, is refused with the fix (chown root, chmod go-w) in the message; an unarchived artifact Greengrass gave to the component's run user is refused with Owned by uid N (open decision 13).
  - The migration CLI reports a refusal on stderr, naming the path and the failed condition, and exits with code 2; programmatic callers get the exception. Its outputs are unchanged: only the JSON and allow_pickle=False NumPy sidecars, with --reference-image-map-file still choosing the output base.
- `95141da5-9489-42a9-a8d5-e066182a8f68-0` (`B310`, task 3):
  - --portal-api / PORTAL_API must be an https URL with a host and no user information, query or fragment; any other value stops the script with exit code 2 and a message naming only the rejected scheme or rule. --dry-run is unchanged.
  - Redirects aren't followed: a 3xx from the Portal API is reported as that HTTP status and the script exits 1, so the Authorization header never reaches a redirect target.
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-2` (`python.lang.security.deserialization.avoid-dill`, task 4):
  - A legacy reference-image map is loaded only through open_trusted_legacy_map: a file that resolves outside TRUSTED_LEGACY_MAP_ROOTS (/aws_dda/greengrass/v2/packages/artifacts-unarchived and /aws_dda/dda_triton/triton_model_repo), isn't a regular file, is owned by a uid other than root or the effective uid, or is writable by group or others is refused with UntrustedLegacyMapError before the deserializer is imported, and nothing is loaded or written. A group-writable map in the Triton model repository, on a station where the installer's 770 pass covered /aws_dda/dda_triton, is refused with the fix (chown root, chmod go-w) in the message; an unarchived artifact Greengrass gave to the component's run user is refused with Owned by uid N (open decision 13).
  - The migration CLI reports a refusal on stderr, naming the path and the failed condition, and exits with code 2; programmatic callers get the exception. Its outputs are unchanged: only the JSON and allow_pickle=False NumPy sidecars, with --reference-image-map-file still choosing the output base.
- `490a14e8-75e3-48e4-af23-ae95676b238c-0` (`python.jwt.security.unverified-jwt-decode`, task 1):
  - An empty ALLOWED_AUDIENCES now denies every request.
  - Issuers must be exact `https://` strings.
  - Cognito access tokens are denied.
  - Tokens without exp, aud or sub are denied.
  - Tokens that pass every check are now allowed: at 4a3f960 construct_rsa_key failed on every token (it called serialize(), which cryptography's RSA public key doesn't provide), so the function denied every request; it now calls public_bytes(). Logged as a design deviation in the design change log.
- `f5f42e1f-3dee-45af-98a9-08715ba0e7d8-5` (`python.lang.security.deserialization.avoid-dill`, task 4):
  - A legacy reference-image map is loaded only through open_trusted_legacy_map: a file that resolves outside TRUSTED_LEGACY_MAP_ROOTS (/aws_dda/greengrass/v2/packages/artifacts-unarchived and /aws_dda/dda_triton/triton_model_repo), isn't a regular file, is owned by a uid other than root or the effective uid, or is writable by group or others is refused with UntrustedLegacyMapError before the deserializer is imported, and nothing is loaded or written. A group-writable map in the Triton model repository, on a station where the installer's 770 pass covered /aws_dda/dda_triton, is refused with the fix (chown root, chmod go-w) in the message; an unarchived artifact Greengrass gave to the component's run user is refused with Owned by uid N (open decision 13).
  - The migration CLI reports a refusal on stderr, naming the path and the failed condition, and exits with code 2; programmatic callers get the exception. Its outputs are unchanged: only the JSON and allow_pickle=False NumPy sidecars, with --reference-image-map-file still choosing the output base.
- `69ef51f8-d65b-4291-a4ae-d447bbd2367d-0` (`python.lang.security.audit.dangerous-subprocess-use-audit`, task 2):
  - A custom Python handler path whose real path lies outside the component artifact directory (an absolute path, a '..' segment or a symlink leading out) fails that workflow run with a CustomPythonNodeError naming the node, before any handler process starts.
- `24e13eb5-0be6-4586-abc1-9ca6aa3b9aab-0` (`python.lang.security.audit.dangerous-subprocess-use-audit`, task 2):
  - A Folder image source whose location resolves outside /aws_dda, to /aws_dda itself (such as a device-UI entry of '.'), or into /aws_dda/greengrass or /aws_dda/system is refused with HTTP 400 when it is created or updated, and nothing is created. A change applied through the camera-registry sync fails with that message as its reason. Stored sources aren't re-validated, and reading from them doesn't change.
  - A shadow workflow id whose results path (/aws_dda/inference-results/<id>) resolves outside /aws_dda, to /aws_dda itself, or into /aws_dda/greengrass or /aws_dda/system isn't created. Its error also stops the desired ids after it in that shadow document and the document's metadata writes, on every delivery until the id leaves the desired state. An id that leaves the results directory but resolves inside that area, such as '../captures' (/aws_dda/captures), is still created and its directory walked, as at 4a3f960.
  - A uid or gid (DDA_*_USER_ID, DDA_*_GROUP_ID) that is set but isn't 1 to 10 decimal digits stops container startup with a ValueError from setup_dda_users_and_groups. An empty or unset id isn't checked.

## Owner decisions recorded by triage

- bandit-B301: Optional alternative to confining the legacy-map read: drop the offline migration utility from the shipped images if no legacy maps remain in use. This removes a function, so it needs owner approval; it is not the default plan.
- bandit-B310: payload_fetch.py keeps its documented rule that an empty allowed_uri_prefixes permits every remote source (bedrock payload reference Requirement 3.4). Changing to default-deny would break existing nodes configured without prefixes, so it is left as residual risk rather than proposed.
- checkov-dynamodb: A customer managed key adds a monthly key charge plus request charges (requirements open question 2). Cost alone is not impaired function (R4.5), so the owner confirms the key before implementation; one key can serve both flagged tables.
- checkov-iam: Approve how the IAM fixtures change when these statements are scoped (R15.6, R16.3). Scoping removes wildcard grant atoms that iam_baseline_EdgeCVPortalComputeStack.template.json and iam_baseline_DDAPortalUseCaseAccountStack.template.json record, which test_synth_iam_statements_match_fixed_baseline rejects and iam_post_fix_approved_additions.json cannot excuse. Refreshing those Baseline_Templates changes the difference that test_baseline_drift_confined_to_I1_I4 pins to iam_baseline_cdk_i_changes.json, and the Unfixed_Snapshots may not be edited (R16.4). The design proposes the path; the owner approves it before any fixture or approval file changes.
- checkov-sns: Key choice for the design and owner: alias/aws/sns has no monthly key charge and is enough for the two same-account Lambda publishers. A customer managed key is only required if an operator has wired an AWS service publisher (CloudWatch alarm, EventBridge rule) to the exported topic ARN outside the repo, which the code cannot show; a customer managed key adds monthly key and request charges (requirements open question 2; cost alone is not impairment, R4.5).
- checkov-sqs: Encryption choice per queue (see reason) and, where SSE-SQS is chosen, how the residual CKV_AWS_27 on rescan is closed. Checkov's CloudFormation CKV_AWS_27 passes only when Properties/KmsMasterKeyId is set: SqsManagedSseEnabled is not read (checkov/cloudformation/checks/resource/aws/SQSQueueEncryption.py in 3.2.255 and on the current main branch; public Checkov issue #5869). A local Checkov 3.2.255 probe confirmed SqsManagedSseEnabled: true still fails and KmsMasterKeyId alias/aws/sqs passes.
- semgrep-dill: Optional alternative to confining the legacy-map read: drop the offline migration utility from the shipped images and the staged test-sandbox copy if no legacy maps remain in use. This removes the conversion path that the postprocessor's error message points operators to, so it needs owner approval; it is not the default plan. Same alternative as in the Bandit B301 partial.
- semgrep-jwt: Alternative to hardening the authorizer: delete the unattached JwtAuthorizerHandler (edge-cv-portal/infrastructure/lib/compute-stack.ts:1259-1272) and jwt_authorizer.py, since no API invokes it. This removes the documented 'alternative to Cognito authorizer' path for custom identity providers, so it needs owner approval; hardening is the default plan.
- semgrep-subprocess: utils.py:157 remediation: the allowed-root list for Folder image-source locations must cover every location stations already use. The triage found no inventory of configured locations or of the container's host mounts. If the list would exclude an existing source, the owner decides between widening the list and migrating that source.
