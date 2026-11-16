import importlib
import json
import os
import sys
from pathlib import Path
from decimal import Decimal
import boto3
import pytest
import yaml
from moto import mock_aws
from boto3.dynamodb.types import TypeSerializer

ROOT = Path(__file__).parent
for folder in ('network-telemetry', 'coverage-geospatial', 'notifications'):
    sys.path.insert(0, str(ROOT / folder / 'handlers'))

@pytest.fixture
def aws(monkeypatch):
    for key, value in {'AWS_ACCESS_KEY_ID': 'testing', 'AWS_SECRET_ACCESS_KEY': 'testing', 'AWS_DEFAULT_REGION': 'us-east-1',
                       'AWS_REGION': 'us-east-1', 'AWS_EC2_METADATA_DISABLED': 'true', 'DELIVERY_ENABLED': 'false',
                       'GEOSPATIAL_BUCKET': 'rtc-test-geospatial', 'HISTORY_BUCKET': 'rtc-test-history', 'LOCAL_TEST': 'true',
                       'INFRASTRUCTURE_API_URL': 'https://infrastructure.invalid', 'IDENTITY_API_URL': 'https://identity.invalid',
                       'REPORTS_API_URL': 'https://reports.invalid', 'FRONTEND_ORIGIN': 'https://frontend.invalid',
                       'SES_FROM_ADDRESS': 'sender@example.com', 'DELIVERY_TOPIC_ARN': 'arn:aws:sns:us-east-1:123456789012:delivery',
                       'LATEST_TABLE': 'rtc-telemetry-latest-dev', 'COVERAGE_TABLE': 'rtc-coverage-dev'}.items():
        monkeypatch.setenv(key, value)
    with mock_aws():
        db = boto3.resource('dynamodb', region_name='us-east-1')
        for folder in ('network-telemetry', 'coverage-geospatial', 'notifications'):
            template = yaml.safe_load((ROOT / folder / 'serverless.yml').read_text(encoding='utf-8'))
            for resource in template['resources']['Resources'].values():
                if resource['Type'] != 'AWS::DynamoDB::Table':
                    continue
                props = resource['Properties']
                options = {k: v for k, v in props.items() if k in ('KeySchema', 'AttributeDefinitions', 'GlobalSecondaryIndexes', 'StreamSpecification', 'BillingMode')}
                if 'StreamSpecification' in options:
                    options['StreamSpecification'] = dict(options['StreamSpecification'], StreamEnabled=True)
                table_name = props['TableName']
                if isinstance(table_name, dict):
                    table_name = table_name['Fn::Sub']
                if table_name.startswith('${self:provider.environment.'):
                    env_name = table_name[len('${self:provider.environment.'):-1]
                    table_name = template['provider']['environment'][env_name]
                db.create_table(TableName=table_name.replace('${sls:stage}', 'dev'), **options)
        s3 = boto3.client('s3')
        for bucket in ('rtc-test-geospatial', 'rtc-test-history'):
            s3.create_bucket(Bucket=bucket)
        sqs = boto3.client('sqs')
        for key, name in [('INGEST_QUEUE_URL', 'ingest'), ('SEND_QUEUE_URL', 'send')]:
            monkeypatch.setenv(key, sqs.create_queue(QueueName=name)['QueueUrl'])
        for module_name in ['telemetry_common', 'geo_common', 'notification_common', 'oracle_spatial']:
            sys.modules.pop(module_name, None)
        yield {'db': db, 's3': s3, 'sqs': sqs}


@pytest.fixture
def load(aws, monkeypatch):
    def module(name):
        result = importlib.reload(sys.modules[name]) if name in sys.modules else importlib.import_module(name)
        if name in ('consume_incident_events', 'post_notification'):
            monkeypatch.setattr(result, 'administrator_ids', lambda: set())
        return result
    return module


@pytest.fixture
def api():
    def make(data=None, user='USR-001', groups=('admin',), query=None, **paths):
        claims = {'sub': user}
        if groups is not None:
            claims['cognito:groups'] = list(groups) if isinstance(groups, tuple) else groups
        return {'body': json.dumps(data or {}), 'headers': {'Idempotency-Key': 'request-1'}, 'pathParameters': paths,
                'queryStringParameters': query or {}, 'requestContext': {'authorizer': {'claims': claims}}}
    return make


@pytest.fixture
def stream_record():
    def make(item, old=None, event_name='INSERT'):
        serializer = TypeSerializer()
        encode = lambda value: {k: serializer.serialize(v) for k, v in value.items()}
        return {'eventName': event_name, 'eventSource': 'aws:dynamodb', 'eventID': 'stream-1',
                'dynamodb': {'NewImage': encode(item), 'OldImage': encode(old or {}), 'SequenceNumber': '1'}}
    return make


def envelope(source, event_type, data, event_id='EVT-001'):
    return {'source': source, 'detail-type': event_type, 'detail': {'source': source, 'event_type': event_type,
        'event_id': event_id, 'version': '1.0', 'occurred_at': '2026-11-14T12:00:00Z', 'data': data}}
