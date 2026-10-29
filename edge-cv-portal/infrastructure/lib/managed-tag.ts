import * as cdk from 'aws-cdk-lib';
import { IConstruct } from 'constructs';

/** The portal's ownership tag: dda-portal:managed=true. */
export const MANAGED_TAG_KEY = 'dda-portal:managed';
export const MANAGED_TAG_VALUE = 'true';

/**
 * The resource types the CloudFormation execution policies manage only when
 * they carry the managed tag (iam/portal-cfn-execution-app-policy.json for KMS
 * keys, iam/portal-cfn-execution-network-policy.json for the network
 * resources). The execution role may create one of these only with the tag in
 * the create request. Keep this list, the policies and MANAGED_TAG_TYPES in
 * iam/setup-account.sh (which tags those an earlier deploy created) in step.
 *
 * Route table associations, routes, gateway attachments, security group rules
 * and KMS aliases have no tags of their own; the policies authorize them
 * through the tagged route table, subnet, VPC, gateway, security group or key.
 */
export const EXEC_POLICY_TAGGED_RESOURCE_TYPES = [
  'AWS::KMS::Key',
  'AWS::EC2::VPC',
  'AWS::EC2::Subnet',
  'AWS::EC2::RouteTable',
  'AWS::EC2::InternetGateway',
  'AWS::EC2::SecurityGroup',
  'AWS::EC2::VPCEndpoint',
];

/** Tag every KMS key and network resource under `scope` with dda-portal:managed=true. */
export function tagExecPolicyManagedResources(scope: IConstruct): void {
  cdk.Tags.of(scope).add(MANAGED_TAG_KEY, MANAGED_TAG_VALUE, {
    includeResourceTypes: EXEC_POLICY_TAGGED_RESOURCE_TYPES,
  });
}
