"""
Shared DynamoDB test helpers.

`conftest.aws_stack` is session-scoped, so one moto DynamoDB backend is
shared by the whole directory run and accumulates every module's tables.
DynamoDB's ListTables returns at most 100 names per page (moto: insertion
order), so a create-if-missing guard written as
`if NAME not in client.list_tables()["TableNames"]` silently stops seeing
tables created after the hundredth and then fails with
`ResourceInUseException: Table already exists`. Every such guard must go
through `all_table_names`, which pages to the end.
"""


def all_table_names(client):
    """Every table name the DynamoDB client can see, across all pages."""
    names = []
    kwargs = {}
    while True:
        response = client.list_tables(**kwargs)
        names.extend(response.get("TableNames", []))
        last = response.get("LastEvaluatedTableName")
        if not last:
            return names
        kwargs = {"ExclusiveStartTableName": last}
