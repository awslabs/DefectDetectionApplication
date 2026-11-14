import * as cdk from 'aws-cdk-lib';
import * as iam from 'aws-cdk-lib/aws-iam';

/**
 * The CDK context value naming the DDA permissions boundary
 * (-c ddaPermissionsBoundary=<policy name>). The deploy scripts pass it when
 * the account was bootstrapped with that boundary
 * (scripts/portal-permissions-boundary.sh, iam/setup-account.sh):
 * dda-portal-permissions-boundary for the portal app (bin/app.ts),
 * dda-usecase-permissions-boundary and dda-data-permissions-boundary for the
 * UseCase and Data account apps.
 */
export const PERMISSIONS_BOUNDARY_CONTEXT_KEY = 'ddaPermissionsBoundary';

const POLICY_NAME = /^[\w+=,.@-]{1,128}$/;

/**
 * Put the customer managed policy named by the ddaPermissionsBoundary context
 * value on every IAM role of every stack in `app` (nested stacks and custom
 * resource provider roles included). Call it after every stack is created.
 *
 * The boundary on the CloudFormation execution role refuses to create a role
 * without the same boundary, so in an account bootstrapped with it every role
 * the app creates must carry it. Without the context value nothing changes.
 *
 * @returns the policy name, or undefined when the context names none.
 */
export function applyDdaPermissionsBoundary(app: cdk.App): string | undefined {
  const name = String(app.node.tryGetContext(PERMISSIONS_BOUNDARY_CONTEXT_KEY) ?? '').trim();
  if (!name) return undefined;
  if (!POLICY_NAME.test(name)) {
    throw new Error(`${PERMISSIONS_BOUNDARY_CONTEXT_KEY} must be an IAM policy name, got '${name}'`);
  }
  for (const stack of app.node.children.filter(cdk.Stack.isStack)) {
    iam.PermissionsBoundary.of(stack).apply(
      iam.ManagedPolicy.fromManagedPolicyName(stack, 'DdaPermissionsBoundary', name));
  }
  return name;
}
