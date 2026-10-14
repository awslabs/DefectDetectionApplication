/**
 * Paths held out of the fingerprint of every Python container image asset.
 *
 * CDK hashes the entire build context to decide whether an image has to be
 * rebuilt. Python bytecode caches are untracked build droppings that appear in
 * those contexts whenever anyone runs the backend tests, so without this list
 * the asset hash of a worker whose source never changed still moves. cdk-assets
 * then finds no matching tag in ECR and a deploy that should have been a
 * configuration change becomes a multi-gigabyte image build — for the
 * grounded-sam worker that means re-downloading the Grounding DINO ONNX export,
 * its tokenizer and the SAM archive, which stalled two portal deploys for hours
 * before the cause was understood.
 *
 * Measured on backend/grounded-sam-worker with aws-cdk-lib 2.229.1:
 *
 *   clean context                        2bf7db83…
 *   + one stray __pycache__/*.pyc        399acd60…   <- phantom rebuild
 *   + this exclude list, pyc present     2bf7db83…   <- back to the clean hash
 *
 * A `.dockerignore` does not solve this: `@aws-cdk/core:dockerIgnoreSupport` is
 * not enabled in cdk.json, so the file is not applied to the fingerprint and is
 * instead hashed as one more context file (325eb2f8…) — which would force
 * exactly the rebuild this list exists to avoid. Excluding at the call site is
 * what keeps the hash stable.
 *
 * Adopting this list on an asset whose context is currently clean does not
 * change that asset's hash, because the excluded files are ones that must never
 * reach the image anyway. That is why it could be introduced without forcing a
 * rebuild of the published workers.
 *
 * Keep every image asset in this repo on this list. An asset that omits it is
 * one stray test run away from a surprise multi-gigabyte deploy.
 */
export const PYTHON_CONTAINER_ASSET_EXCLUDES: string[] = [
  '__pycache__/**',
  '**/__pycache__/**',
  '**/*.pyc',
  '**/*.pyo',
  '**/.pytest_cache/**',
  '**/.mypy_cache/**',
];
