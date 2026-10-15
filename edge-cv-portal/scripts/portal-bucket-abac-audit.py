#!/usr/bin/env python3
"""Read-only compliance audit of the S3 buckets the portal's use cases register.

Lists every bucket of the Lifecycle_Set (each use case's s3_bucket,
data_s3_bucket, additional_s3_buckets entries and inference_uploader_s3_bucket)
and, for each bucket the portal reaches on a Lambda execution role (a
Portal_Account_Bucket), reports whether it carries dda-portal:managed=true,
whether ABAC is enabled on it, its region, and compliance (tagged and ABAC
enabled). Buckets reached through a cross-account role are listed as not
probed; the portal's own buckets (artifacts, component, inference results) are
excluded by name. A second scan of the training-jobs table reports stored
model artifacts whose bucket is outside the Lifecycle_Set
(legacy-artifact-bucket) for single-account use cases.
(portal-authorization-and-iam-scoping, task 5.1; design I5 "Existing
registered buckets".)

Read-only by default: DynamoDB Scan, S3 GetBucketTagging / GetBucketAbac /
GetBucketLocation, STS GetCallerIdentity, and, only when the table names are
not given, CloudFormation DescribeStacks; Lambda GetFunctionConfiguration only
with --model-converter-function. `--apply --buckets <names>` (the backfill
runbook only) tags the named non-compliant Portal_Account_Buckets with S3
Control TagResource and enables ABAC with PutBucketAbac, refusing any name the
read-only pass did not list as a Portal_Account_Bucket.

Runs on the developer's Python 3.9+ and local boto3 (which must know
GetBucketAbac); it loads the shared layer's is_cross_account_setup by file
path and never puts the layer directory on sys.path. Account ids are masked
in the output.

Exit status: 0 every Portal_Account_Bucket compliant and no
legacy-artifact-bucket; 1 findings (or an --apply call failed); 2 usage
error, refused --apply, or a local boto3 without the ABAC operations.
"""
import argparse
import importlib.util
import json
import os
import re
import sys
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

STORAGE_STACK = 'EdgeCVPortalStorageStack'
USECASES_TABLE_OUTPUT = 'UseCasesTableName'
TRAINING_JOBS_TABLE_OUTPUT = 'TrainingJobsTableName'
FALLBACK_USECASES_TABLE = 'dda-portal-usecases'
FALLBACK_TRAINING_JOBS_TABLE = 'dda-portal-training-jobs'

LIFECYCLE_FIELDS = ('s3_bucket', 'data_s3_bucket', 'additional_s3_buckets',
                    'inference_uploader_s3_bucket')
# The Lifecycle_Set fields plus what the single-account predicate and the
# selector's separate-data-account rule need.
USECASE_PROJECTION = ('usecase_id',) + LIFECYCLE_FIELDS + (
    'account_id', 'cross_account_role_arn', 'data_account_id')
TRAINING_PROJECTION = ('training_id', 'usecase_id', 'source', 'artifact_s3',
                       'model_source.s3_model_artifact')
ARTIFACT_FIELDS = ('artifact_s3', 'model_source.s3_model_artifact')

MANAGED_TAG_KEY = 'dda-portal:managed'
MANAGED_TAG_VALUE = 'true'
RESULTS_BUCKET_PREFIX = 'dda-inference-results-'
BUCKET_NAME_RE = re.compile(r'^[A-Za-z0-9._-]{3,255}$')
ACCOUNT_RE = re.compile(r'(?<![0-9])[0-9]{12}(?![0-9])')

HERE = os.path.dirname(os.path.abspath(__file__))
LAYER_DIR = os.path.normpath(os.path.join(HERE, '..', 'backend', 'layers', 'shared', 'python'))

KIND_PORTAL = 'portal-account'
KIND_CROSS = 'cross-account'
KIND_PORTAL_OWN = 'listed-portal-own'


def mask(text: Any) -> str:
    """Mask every twelve-digit account id to its last four digits."""
    return ACCOUNT_RE.sub(lambda m: '********' + m.group(0)[-4:], str(text))


def load_shared_layer(layer_dir: str = LAYER_DIR, region: str = 'us-east-1') -> Any:
    """Load the shared layer's shared_utils.py by file path (after its one
    sibling import, internal_invocation.py) without adding the layer directory
    to sys.path, so the host's own boto3 serves it. Registered under a private
    module name so an importer's own `shared_utils` is never replaced. The
    module builds a DynamoDB resource at import, so a default region is set
    when the environment has none."""
    os.environ.setdefault('AWS_DEFAULT_REGION', region)
    if 'internal_invocation' not in sys.modules:
        sys.modules['internal_invocation'] = _load_module(
            'internal_invocation', os.path.join(layer_dir, 'internal_invocation.py'))
    return _load_module('portal_audit_shared_utils', os.path.join(layer_dir, 'shared_utils.py'))


def _load_module(name: str, path: str) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def portal_own_buckets(account_id: str, region: str,
                       artifacts_bucket: Optional[str] = None) -> Dict[str, str]:
    """The Portal_Own_Buckets by name: role -> bucket."""
    return {
        'artifacts': artifacts_bucket or f'dda-portal-artifacts-{account_id}-{region}',
        'component': f'dda-component-{region}-{account_id}',
        'inference-results': f'{RESULTS_BUCKET_PREFIX}{account_id}',
    }


def bucket_from_uri(uri: Any) -> Optional[str]:
    """The bucket of an `s3://bucket[/key]` URI, or None when it is malformed
    (not a string, another scheme, or no valid bucket name)."""
    if not isinstance(uri, str) or not uri.startswith('s3://'):
        return None
    bucket = uri[len('s3://'):].partition('/')[0]
    return bucket if BUCKET_NAME_RE.fullmatch(bucket) else None


def _get_path(item: Dict[str, Any], dotted: str) -> Any:
    value: Any = item
    for part in dotted.split('.'):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def scan_all(table: Any, projection: Iterable[str]) -> List[Dict[str, Any]]:
    """Every item of `table` with only `projection` (dotted paths allowed);
    every name goes through ExpressionAttributeNames (`source` is a reserved
    word)."""
    names: Dict[str, str] = {}
    paths = []
    for path in projection:
        parts = []
        for part in path.split('.'):
            alias = f'#a{len(names)}'
            names[alias] = part
            parts.append(alias)
        paths.append('.'.join(parts))
    kwargs: Dict[str, Any] = {'ProjectionExpression': ', '.join(paths),
                              'ExpressionAttributeNames': names}
    items: List[Dict[str, Any]] = []
    while True:
        response = table.scan(**kwargs)
        items.extend(response.get('Items', []))
        last_key = response.get('LastEvaluatedKey')
        if not last_key:
            return items
        kwargs['ExclusiveStartKey'] = last_key


def _field_buckets(record: Dict[str, Any], field: str) -> List[str]:
    value = record.get(field)
    values = value if isinstance(value, (list, tuple, set)) else [value]
    return [name for name in values if isinstance(name, str) and name.strip()]


def is_portal_account_listing(record: Dict[str, Any], field: str,
                              is_cross_account: Callable[[Dict[str, Any]], Any]) -> bool:
    """True when the portal reaches this listing's bucket on a Lambda execution
    role: the record is a Single_Account_Record and the bucket is not the
    data_s3_bucket of a separate data account (which the selector reaches
    through the data account's role)."""
    if is_cross_account(record):
        return False
    if field == 'data_s3_bucket':
        data_account = record.get('data_account_id')
        if data_account and data_account != record.get('account_id'):
            return False
    return True


def classify_lifecycle(usecases: List[Dict[str, Any]], own: Iterable[str],
                       is_cross_account: Callable[[Dict[str, Any]], Any]) -> Dict[str, Dict[str, Any]]:
    """bucket -> {'kind', 'usecases', 'fields'} over the Lifecycle_Set. A bucket
    any single-account listing reaches on the execution role is a
    Portal_Account_Bucket even when a multi-account record also lists it."""
    own_names = set(own)
    buckets: Dict[str, Dict[str, Any]] = {}
    for record in usecases:
        for field in LIFECYCLE_FIELDS:
            for bucket in _field_buckets(record, field):
                entry = buckets.setdefault(bucket, {'kind': KIND_CROSS, 'usecases': set(),
                                                    'fields': set()})
                entry['usecases'].add(record.get('usecase_id') or 'unknown')
                entry['fields'].add(field)
                if bucket in own_names:
                    entry['kind'] = KIND_PORTAL_OWN
                elif entry['kind'] != KIND_PORTAL_OWN and is_portal_account_listing(
                        record, field, is_cross_account):
                    entry['kind'] = KIND_PORTAL
    return buckets


def _error_code(error: Exception) -> str:
    response = getattr(error, 'response', None) or {}
    return str((response.get('Error') or {}).get('Code') or type(error).__name__)


def probe_bucket(clients: Any, bucket: str) -> Dict[str, Any]:
    """Read-only: region, managed tag and ABAC status of one bucket."""
    result: Dict[str, Any] = {'region': None, 'tagged': False, 'abac_enabled': False,
                              'compliant': False, 'error': None}
    try:
        location = clients.s3(None).get_bucket_location(Bucket=bucket).get('LocationConstraint')
        result['region'] = {None: 'us-east-1', '': 'us-east-1', 'EU': 'eu-west-1'}.get(
            location, location)
        regional = clients.s3(result['region'])
        try:
            tag_set = regional.get_bucket_tagging(Bucket=bucket).get('TagSet', [])
        except Exception as error:
            if _error_code(error) != 'NoSuchTagSet':
                raise
            tag_set = []
        tags = {tag.get('Key'): tag.get('Value') for tag in tag_set if isinstance(tag, dict)}
        result['tagged'] = tags.get(MANAGED_TAG_KEY) == MANAGED_TAG_VALUE
        status = (regional.get_bucket_abac(Bucket=bucket).get('AbacStatus') or {}).get('Status')
        result['abac_enabled'] = status == 'Enabled'
    except Exception as error:
        result['error'] = _error_code(error)
    result['compliant'] = bool(result['tagged'] and result['abac_enabled'] and not result['error'])
    return result


def results_override_counts(usecases: List[Dict[str, Any]]) -> Tuple[int, int]:
    """(use cases with a Results_Bucket_Override, those outside dda-inference-results-*)."""
    overrides = [b for record in usecases
                 for b in _field_buckets(record, 'inference_uploader_s3_bucket')]
    return len(overrides), sum(1 for b in overrides if not b.startswith(RESULTS_BUCKET_PREFIX))


def classify_artifacts(training_items: List[Dict[str, Any]], usecases_by_id: Dict[str, Dict[str, Any]],
                       lifecycle: Iterable[str], own: Iterable[str],
                       is_cross_account: Callable[[Dict[str, Any]], Any]) -> Dict[str, Any]:
    """The stored-artifact classes of the training-jobs scan:
    `legacy` (bucket outside the Lifecycle_Set and the Portal_Own_Buckets,
    owner a Single_Account_Record), `not_probed` (owner multi-account),
    `orphans` (no owning use case), `unparseable` (malformed URI)."""
    lifecycle_set, own_set = set(lifecycle), set(own)
    legacy: Dict[str, Dict[str, Any]] = {}
    not_probed: Dict[str, Dict[str, Any]] = {}
    orphans: List[str] = []
    unparseable: List[Tuple[str, str]] = []
    for item in training_items:
        training_id = str(item.get('training_id') or 'unknown')
        uris = [(field, _get_path(item, field)) for field in ARTIFACT_FIELDS]
        uris = [(field, uri) for field, uri in uris if uri not in (None, '')]
        if not uris:
            continue
        owner = usecases_by_id.get(item.get('usecase_id'))
        if owner is None:
            orphans.append(training_id)
            continue
        for field, uri in uris:
            bucket = bucket_from_uri(uri)
            if bucket is None:
                unparseable.append((training_id, field))
                continue
            if bucket in lifecycle_set or bucket in own_set:
                continue
            target = not_probed if is_cross_account(owner) else legacy
            entry = target.setdefault(bucket, {'records': set(), 'usecases': set()})
            entry['records'].add(training_id)
            entry['usecases'].add(owner.get('usecase_id') or 'unknown')
    return {'legacy': legacy, 'not_probed': not_probed, 'orphans': orphans,
            'unparseable': unparseable}


class AwsClients:
    """boto3 clients for one session; S3 and S3 Control clients per region."""

    def __init__(self, profile: Optional[str], region: Optional[str]):
        import boto3  # local boto3 on the developer's machine
        self.session = boto3.session.Session(profile_name=profile, region_name=region)
        self.region = region or self.session.region_name or 'us-east-1'
        self._cache: Dict[Tuple[str, Optional[str]], Any] = {}

    def _client(self, service: str, region: Optional[str]) -> Any:
        key = (service, region)
        if key not in self._cache:
            self._cache[key] = self.session.client(service, region_name=region or self.region)
        return self._cache[key]

    def s3(self, region: Optional[str]) -> Any:
        return self._client('s3', region)

    def s3control(self, region: Optional[str]) -> Any:
        return self._client('s3control', region)

    def sts(self) -> Any:
        return self._client('sts', None)

    def lambda_(self) -> Any:
        return self._client('lambda', None)

    def cloudformation(self) -> Any:
        return self._client('cloudformation', None)

    def table(self, name: str) -> Any:
        return self.session.resource('dynamodb', region_name=self.region).Table(name)


def stack_table_names(clients: Any) -> Dict[str, str]:
    """The storage stack's table-name outputs, or {} when unreadable."""
    try:
        stacks = clients.cloudformation().describe_stacks(StackName=STORAGE_STACK).get('Stacks', [])
    except Exception:
        return {}
    outputs = (stacks[0].get('Outputs') if stacks else None) or []
    return {o.get('OutputKey'): o.get('OutputValue') for o in outputs if o.get('OutputKey')}


def trusted_model_line(clients: Any, function_name: Optional[str]) -> str:
    if not function_name:
        return 'ModelConverter TRUSTED_MODEL_BUCKETS / TRUSTED_MODEL_ACCOUNTS: not checked'
    try:
        config = clients.lambda_().get_function_configuration(FunctionName=function_name)
    except Exception as error:
        return f'ModelConverter TRUSTED_MODEL_BUCKETS / TRUSTED_MODEL_ACCOUNTS: unreadable ({_error_code(error)})'
    variables = (config.get('Environment') or {}).get('Variables') or {}
    return ('ModelConverter TRUSTED_MODEL_BUCKETS={!r} TRUSTED_MODEL_ACCOUNTS={!r}'.format(
        variables.get('TRUSTED_MODEL_BUCKETS', ''), variables.get('TRUSTED_MODEL_ACCOUNTS', '')))


def audit(clients: Any, *, usecases_table: str, training_jobs_table: str, account_id: str,
          region: str, artifacts_bucket: Optional[str], is_cross_account: Callable[[Dict[str, Any]], Any],
          model_converter_function: Optional[str] = None) -> Dict[str, Any]:
    """The read-only pass. Returns the report the printer and --apply use."""
    usecases = scan_all(clients.table(usecases_table), USECASE_PROJECTION)
    own = portal_own_buckets(account_id, region, artifacts_bucket)
    lifecycle = classify_lifecycle(usecases, own.values(), is_cross_account)
    probes = {bucket: probe_bucket(clients, bucket)
              for bucket, entry in sorted(lifecycle.items()) if entry['kind'] == KIND_PORTAL}
    training_items = scan_all(clients.table(training_jobs_table), TRAINING_PROJECTION)
    usecases_by_id = {record.get('usecase_id'): record for record in usecases if record.get('usecase_id')}
    artifacts = classify_artifacts(training_items, usecases_by_id, lifecycle, own.values(),
                                   is_cross_account)
    return {'usecases': len(usecases), 'own': own, 'lifecycle': lifecycle, 'probes': probes,
            'overrides': results_override_counts(usecases), 'artifacts': artifacts,
            'training_records': len(training_items),
            'trusted_model': trusted_model_line(clients, model_converter_function)}


def print_report(report: Dict[str, Any], out: Callable[[str], None]) -> int:
    """Print the compliance table and the summaries; return the exit status."""
    lifecycle, probes, artifacts = report['lifecycle'], report['probes'], report['artifacts']
    out(f"use cases scanned: {report['usecases']}; Lifecycle_Set buckets: {len(lifecycle)}")
    out('bucket | kind | use cases | region | tagged | abac_enabled | compliant')
    for bucket, entry in sorted(lifecycle.items()):
        ids = ','.join(sorted(entry['usecases']))
        if entry['kind'] == KIND_PORTAL:
            probe = probes[bucket]
            out(f"{bucket} | {KIND_PORTAL} | {ids} | {probe['region'] or '-'} | {probe['tagged']} | "
                f"{probe['abac_enabled']} | {probe['compliant']}"
                + (f" | error {probe['error']}" if probe['error'] else ''))
        elif entry['kind'] == KIND_PORTAL_OWN:
            out(f"{bucket} | {KIND_PORTAL_OWN} | {ids} | - | - | - | informational (granted by name)")
        else:
            out(f"{bucket} | {KIND_CROSS} | {ids} | - | - | - | not probed (cross-account role)")
    non_compliant = sorted(b for b, p in probes.items() if not p['compliant'])
    out(f"Portal_Account_Buckets: {len(probes)}; compliant: {len(probes) - len(non_compliant)}; "
        f"non-compliant: {len(non_compliant)}" + (f" ({', '.join(non_compliant)})" if non_compliant else ''))
    total, outside = report['overrides']
    out(f"use cases with a results-bucket override: {total} ({outside} outside {RESULTS_BUCKET_PREFIX}*)")
    out(f"training records scanned: {report['training_records']}")
    for bucket, entry in sorted(artifacts['legacy'].items()):
        out(f"legacy-artifact-bucket {bucket} — {len(entry['records'])} record(s), "
            f"use cases {','.join(sorted(entry['usecases']))}")
    for bucket, entry in sorted(artifacts['not_probed'].items()):
        out(f"informational: {bucket} — {len(entry['records'])} record(s) of multi-account use cases: "
            "not probed (cross-account role)")
    for training_id in artifacts['orphans']:
        out(f"orphan-record {training_id} (no use case record)")
    for training_id, field in artifacts['unparseable']:
        out(f"unparseable {field} on training record {training_id}")
    out(f"legacy-artifact buckets: {len(artifacts['legacy'])}")
    out(report['trusted_model'])
    return 1 if non_compliant or artifacts['legacy'] else 0


def plan_apply(report: Dict[str, Any], names: List[str]) -> Tuple[List[str], List[str]]:
    """(buckets to apply, refusals). Only a Portal_Account_Bucket of the
    scanned Lifecycle_Set may be applied; a legacy-artifact bucket is never in
    that set, so it is refused too."""
    refusals, accepted = [], []
    for name in names:
        entry = report['lifecycle'].get(name)
        if entry is None:
            reason = ('a legacy-artifact bucket (outside the Lifecycle_Set)'
                      if name in report['artifacts']['legacy'] else 'not in the scanned Lifecycle_Set')
            refusals.append(f'{name}: {reason}')
        elif entry['kind'] != KIND_PORTAL:
            refusals.append(f"{name}: not a Portal_Account_Bucket ({entry['kind']})")
        elif report['probes'][name]['region'] is None:
            refusals.append(f'{name}: region unknown in the read-only pass')
        else:
            accepted.append(name)
    return accepted, refusals


def apply_buckets(clients: Any, report: Dict[str, Any], buckets: List[str], *, account_id: str,
                  enable_first: bool, out: Callable[[str], None]) -> int:
    """Tag (S3 Control TagResource) and enable ABAC (PutBucketAbac) on each
    bucket, with clients bound to the bucket's region from the read-only pass."""
    failures = 0
    for bucket in buckets:
        probe = report['probes'][bucket]
        region = probe['region']
        steps = []
        if not probe['tagged']:
            steps.append(('tag', lambda b=bucket, r=region: clients.s3control(r).tag_resource(
                AccountId=account_id, ResourceArn=f'arn:aws:s3:::{b}',
                Tags=[{'Key': MANAGED_TAG_KEY, 'Value': MANAGED_TAG_VALUE}])))
        if not probe['abac_enabled']:
            steps.append(('enable ABAC', lambda b=bucket, r=region: clients.s3(r).put_bucket_abac(
                Bucket=b, AbacStatus={'Status': 'Enabled'})))
        if enable_first:
            steps.reverse()
        if not steps:
            out(f'{bucket}: already compliant; nothing applied')
        for label, step in steps:
            try:
                step()
                out(f'{bucket} ({region}): {label}: done')
            except Exception as error:
                failures += 1
                out(f'{bucket} ({region}): {label}: failed ({_error_code(error)})')
                break
    return 1 if failures else 0


def _has_abac_operations(clients: Any, apply: bool) -> bool:
    """The local SDK knows GetBucketAbac (and, for --apply, PutBucketAbac and
    S3 Control TagResource). Looks the methods up; calls none of them."""
    s3 = clients.s3(None)
    if not callable(getattr(s3, 'get_bucket_abac', None)):
        return False
    if apply:
        return (callable(getattr(s3, 'put_bucket_abac', None))
                and callable(getattr(clients.s3control(None), 'tag_resource', None)))
    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='portal-bucket-abac-audit.py',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            "Read-only compliance audit of the portal use cases' S3 buckets\n"
            '(tagged dda-portal:managed=true and ABAC enabled).\n\n'
            'Table names, when not given:\n'
            f'  use cases:     {STORAGE_STACK} output {USECASES_TABLE_OUTPUT}, '
            f'fallback {FALLBACK_USECASES_TABLE}\n'
            f'  training jobs: {STORAGE_STACK} output {TRAINING_JOBS_TABLE_OUTPUT}, '
            f'fallback {FALLBACK_TRAINING_JOBS_TABLE}\n'
            '  (outputs read with cloudformation describe-stacks)'),
        epilog='Exit status: 0 compliant, 1 findings, 2 usage error or refused --apply.')
    parser.add_argument('--usecases-table',
                        help=f'use-cases table (default: {STORAGE_STACK} output {USECASES_TABLE_OUTPUT}, '
                             f'fallback {FALLBACK_USECASES_TABLE})')
    parser.add_argument('--training-jobs-table',
                        help=f'training-jobs table (default: {STORAGE_STACK} output '
                             f'{TRAINING_JOBS_TABLE_OUTPUT}, fallback {FALLBACK_TRAINING_JOBS_TABLE})')
    parser.add_argument('--profile', help='AWS profile for every client')
    parser.add_argument('--region', help="the portal's region (default: the profile's, else us-east-1)")
    parser.add_argument('--portal-account-id',
                        help='portal account id (default: sts get-caller-identity; printed masked)')
    parser.add_argument('--artifacts-bucket',
                        help='portal artifacts bucket (default: dda-portal-artifacts-<account>-<region>)')
    parser.add_argument('--model-converter-function',
                        help='ModelConverter function name; prints its TRUSTED_MODEL_* variables')
    parser.add_argument('--apply', action='store_true',
                        help='backfill runbook only: tag and enable ABAC on --buckets')
    parser.add_argument('--buckets', nargs='+', default=[],
                        help='with --apply: the non-compliant Portal_Account_Buckets to apply')
    parser.add_argument('--enable-first', action='store_true',
                        help='with --apply: enable ABAC before tagging')
    return parser


def main(argv: Optional[List[str]] = None, *,
         clients_factory: Optional[Callable[[Optional[str], Optional[str]], Any]] = None,
         is_cross_account: Optional[Callable[[Dict[str, Any]], Any]] = None,
         out: Optional[Callable[[str], None]] = None) -> int:
    args = build_parser().parse_args(argv)
    emit = out or (lambda line: print(mask(line)))
    if args.apply and not args.buckets:
        emit('--apply needs --buckets <names>')
        return 2
    if args.buckets and not args.apply:
        emit('--buckets is only used with --apply')
        return 2
    clients = (clients_factory or AwsClients)(args.profile, args.region)
    region = args.region or getattr(clients, 'region', None) or 'us-east-1'
    if not _has_abac_operations(clients, args.apply):
        emit('the local boto3/botocore does not know GetBucketAbac/PutBucketAbac/TagResource; '
             'upgrade it on this machine and re-run')
        return 2
    account_id = args.portal_account_id or clients.sts().get_caller_identity()['Account']
    usecases_table, training_jobs_table = args.usecases_table, args.training_jobs_table
    if not usecases_table or not training_jobs_table:
        outputs = stack_table_names(clients)
        usecases_table = usecases_table or outputs.get(USECASES_TABLE_OUTPUT) or FALLBACK_USECASES_TABLE
        training_jobs_table = (training_jobs_table or outputs.get(TRAINING_JOBS_TABLE_OUTPUT)
                               or FALLBACK_TRAINING_JOBS_TABLE)
    if is_cross_account is None:
        is_cross_account = load_shared_layer(region=region).is_cross_account_setup
    emit(f'portal account {account_id}, region {region}; tables {usecases_table}, {training_jobs_table}')
    report = audit(clients, usecases_table=usecases_table, training_jobs_table=training_jobs_table,
                   account_id=account_id, region=region, artifacts_bucket=args.artifacts_bucket,
                   is_cross_account=is_cross_account,
                   model_converter_function=args.model_converter_function)
    status = print_report(report, emit)
    if not args.apply:
        return status
    accepted, refusals = plan_apply(report, args.buckets)
    if refusals:
        for refusal in refusals:
            emit(f'refused: {refusal}')
        emit('nothing applied')
        return 2
    return apply_buckets(clients, report, accepted, account_id=account_id,
                         enable_first=args.enable_first, out=emit)


if __name__ == '__main__':
    sys.exit(main())
