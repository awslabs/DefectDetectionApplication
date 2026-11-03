/**
 * The MCP SDK Lambda layer source and its bundling (mcp-servers design D19),
 * shared by EdgeCVPortalMcpStack (`McpLayer`) and EdgeCVPortalAssistantStack
 * (`AssistantDepsLayer`; portal-ai-assistant design D1, §10). Both stacks
 * build their own LayerVersion from this one asset, so the pins
 * (`backend/layers/mcp/requirements.lock.txt`: mcp, mangum and their
 * transitive dependencies, among them jsonschema) are identical.
 *
 * Moved out of mcp-stack.ts unchanged; mcp-stack.ts re-exports both names.
 */
import { execFileSync } from 'child_process';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import * as lambda from 'aws-cdk-lib/aws-lambda';

export const MCP_LAYER_SOURCE_DIR = path.join(__dirname, '../../backend/layers/mcp');

/**
 * Local-first bundling of the MCP layer: `backend/layers/mcp/build.sh` with
 * the Docker fallback. It installs exactly the wheels pinned in
 * requirements.lock.txt for CPython 3.11 on x86_64 manylinux2014, and fails
 * when a wheel is missing or a pinned distribution did not land.
 *
 * Fail loud: the local step falls back to Docker only when the host has no
 * pip (build.sh exit 3). Any other build failure throws and fails the synth
 * with build.sh's error, so no deploy can stage an empty layer. The asset
 * hash follows the source (build.sh and the lock), not the bundled output.
 */
export function mcpLayerBundling(): cdk.BundlingOptions {
  const buildScript = path.join(MCP_LAYER_SOURCE_DIR, 'build.sh');
  return {
    image: lambda.Runtime.PYTHON_3_11.bundlingImage,
    command: ['bash', `${cdk.AssetStaging.BUNDLING_INPUT_DIR}/build.sh`, cdk.AssetStaging.BUNDLING_OUTPUT_DIR],
    local: {
      tryBundle(outputDir: string): boolean {
        try {
          // env passed explicitly: the current process.env, also under jest.
          execFileSync('bash', [buildScript, outputDir], {
            env: process.env,
            stdio: ['ignore', 'pipe', 'pipe'],
          });
          return true;
        } catch (err) {
          const failure = err as { status?: number | null; stderr?: Buffer | string };
          if (failure.status === 3) {
            // No pip on this host: bundle in the Docker image instead.
            return false;
          }
          const detail = String(failure.stderr ?? '').trim();
          throw new Error(
            `McpLayer bundling failed: backend/layers/mcp/build.sh exited ${failure.status ?? 'abnormally'}` +
              (detail ? `\n${detail}` : ''),
          );
        }
      },
    },
  };
}
