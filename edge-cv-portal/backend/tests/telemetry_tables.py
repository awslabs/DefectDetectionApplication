"""The four telemetry tables on the conftest moto stack (workflow-telemetry-dashboards
design §7.1-§7.4), shared by the telemetry tests. Not a test module."""
import boto3

REGION = "us-east-1"
SUMMARIES = "test-telemetry-summaries"
EVENTS = "test-telemetry-events"
AGGREGATES = "test-telemetry-aggregates"
CONFIG = "test-telemetry-config"

ENV = {
    "TELEMETRY_SUMMARIES_TABLE": SUMMARIES,
    "TELEMETRY_EVENTS_TABLE": EVENTS,
    "TELEMETRY_AGGREGATES_TABLE": AGGREGATES,
    "TELEMETRY_CONFIG_TABLE": CONFIG,
}

_KEYS = [{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}]
_STRING = lambda name: {"AttributeName": name, "AttributeType": "S"}  # noqa: E731


def ensure_telemetry_tables():
    client = boto3.client("dynamodb", region_name=REGION)
    existing = set(client.list_tables()["TableNames"])
    if SUMMARIES not in existing:
        client.create_table(TableName=SUMMARIES, KeySchema=_KEYS,
                            AttributeDefinitions=[_STRING("pk"), _STRING("sk")], BillingMode="PAY_PER_REQUEST")
    if AGGREGATES not in existing:
        client.create_table(TableName=AGGREGATES, KeySchema=_KEYS,
                            AttributeDefinitions=[_STRING("pk"), _STRING("sk")], BillingMode="PAY_PER_REQUEST")
    if EVENTS not in existing:
        client.create_table(
            TableName=EVENTS, KeySchema=_KEYS,
            AttributeDefinitions=[_STRING("pk"), _STRING("sk"), _STRING("gsi1pk"), _STRING("gsi1sk")],
            GlobalSecondaryIndexes=[{
                "IndexName": "by-workflow",
                "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"}}],
            BillingMode="PAY_PER_REQUEST")
    if CONFIG not in existing:
        client.create_table(
            TableName=CONFIG, KeySchema=_KEYS,
            AttributeDefinitions=[_STRING("pk"), _STRING("sk"), _STRING("gsi1pk"), _STRING("gsi1sk"),
                                  _STRING("gsi2pk"), _STRING("gsi2sk")],
            GlobalSecondaryIndexes=[
                {"IndexName": "by-usecase",
                 "KeySchema": [{"AttributeName": "gsi1pk", "KeyType": "HASH"}, {"AttributeName": "gsi1sk", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "ALL"}},
                {"IndexName": "by-type",
                 "KeySchema": [{"AttributeName": "gsi2pk", "KeyType": "HASH"}, {"AttributeName": "gsi2sk", "KeyType": "RANGE"}],
                 "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["enabled", "account_id", "region"]}},
            ],
            BillingMode="PAY_PER_REQUEST")


def tables():
    resource = boto3.resource("dynamodb", region_name=REGION)
    return {
        "summaries": resource.Table(SUMMARIES),
        "events": resource.Table(EVENTS),
        "aggregates": resource.Table(AGGREGATES),
        "config": resource.Table(CONFIG),
    }


def set_env(monkeypatch):
    for key, value in ENV.items():
        monkeypatch.setenv(key, value)
