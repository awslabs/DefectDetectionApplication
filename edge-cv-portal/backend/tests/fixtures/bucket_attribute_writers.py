"""
Fixture module for the use-case bucket configuration writer guard
(portal-usecase-bucket-config, design §2.4; Requirement 10.10).

`test_usecase_bucket_config_units.py::test_writer_guard_rule_detects_each_shape`
runs the guard's AST rule over this module. One function per write-shaping
position the rule must detect — each contains a DynamoDB write call and
names a bucket attribute in that position — plus functions the rule must
NOT flag: a read-only consumer of `usecase['s3_bucket']` that writes an
unrelated table, and a response shaper that names the attribute without
any write call. Never imported at runtime; `table` and `client` are
placeholders for the boto3 objects the real handlers hold.
"""


def writes_dict_key(table, usecase_id, bucket):
    """Dict-literal key (create_usecase's hand-built item)."""
    table.put_item(Item={'usecase_id': usecase_id, 's3_bucket': bucket})


def writes_field_list(table, usecase_id, body):
    """Element of a list literal (update_usecase's updatable_fields)."""
    updatable_fields = ['name', 'description', 'data_s3_bucket']
    expressions = []
    values = {}
    for field in updatable_fields:
        if field in body:
            expressions.append(f"{field} = :{field}")
            values[f":{field}"] = body[field]
    table.update_item(Key={'usecase_id': usecase_id},
                      UpdateExpression="SET " + ", ".join(expressions),
                      ExpressionAttributeValues=values)


def writes_tuple_element(table, usecase_id, body):
    """Element of a tuple literal (configure_data_account's data_fields)."""
    data_fields = ('data_account_id', 'data_s3_bucket')
    item = {'usecase_id': usecase_id}
    for field in data_fields:
        if field in body:
            item[field] = body[field]
    table.put_item(Item=item)


def writes_subscript_store(table, usecase_id, body):
    """Slice of a subscript STORE (item['data_s3_bucket'] = ...)."""
    item = {'usecase_id': usecase_id}
    item['data_s3_bucket'] = body['bucket']
    table.put_item(Item=item)


def writes_expression_attribute_alias(table, usecase_id, bucket):
    """Value of an ExpressionAttributeNames alias ({'#a': 'additional_s3_buckets'})."""
    table.update_item(Key={'usecase_id': usecase_id},
                      UpdateExpression='SET #a = :b',
                      ExpressionAttributeNames={'#a': 'additional_s3_buckets'},
                      ExpressionAttributeValues={':b': [bucket]})


def writes_update_expression_string(table, usecase_id, bucket):
    """Attribute inside a string literal carrying an update-expression token."""
    table.update_item(
        Key={'usecase_id': usecase_id},
        UpdateExpression='SET additional_s3_buckets = list_append(additional_s3_buckets, :b)',
        ExpressionAttributeValues={':b': [bucket]})


def writes_through_batch_writer(table, records):
    """`batch_writer` counts as a write call."""
    with table.batch_writer() as batch:
        for record in records:
            batch.put_item(Item={'usecase_id': record['id'], 'additional_s3_buckets': []})


def writes_through_transact(client, table_name, usecase_id, bucket):
    """`transact_write_items` counts as a write call."""
    client.transact_write_items(TransactItems=[{
        'Put': {'TableName': table_name,
                'Item': {'usecase_id': {'S': usecase_id}, 's3_bucket': {'S': bucket}}}}])


def reads_bucket_and_writes_elsewhere(table, usecase, job_id):
    """Read-only consumer: `usecase['s3_bucket']` is a subscript LOAD, and the
    write goes to an unrelated table with unrelated keys. Must NOT match."""
    bucket = usecase['s3_bucket']
    manifest = f"s3://{bucket}/manifests/{job_id}.json"
    table.put_item(Item={'job_id': job_id, 'manifest_s3': manifest})


def shapes_response_without_writing(usecase):
    """Names the attribute as a dict key but performs no write. Must NOT match."""
    return {'usecase_id': usecase['usecase_id'], 's3_bucket': usecase.get('s3_bucket'),
            'additional_s3_buckets': list(usecase.get('additional_s3_buckets') or [])}


def filters_by_bucket_without_writing(table, bucket):
    """A read Scan whose filter string names the attribute next to `contains(`;
    no write call, so it must NOT match."""
    return table.scan(
        ProjectionExpression='usecase_id',
        FilterExpression='s3_bucket = :b OR contains(additional_s3_buckets, :b)',
        ExpressionAttributeValues={':b': bucket})
