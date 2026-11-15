"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import re
from decimal import Decimal, Context
from datetime import datetime, timezone
import hashlib
import os
import boto3
from botocore.exceptions import ClientError
from boto3.dynamodb.types import TypeSerializer
import json
import logging
HISTORY = os.environ.get('HISTORY_TABLE', 'rtc-telemetry-history-index-dev')
LATEST = os.environ.get('LATEST_TABLE', 'rtc-telemetry-latest-dev')
dynamodb = boto3.resource('dynamodb')
serializer = TypeSerializer()
client = boto3.client('dynamodb')
ALERTS = os.environ.get('ALERTS_TABLE', 'rtc-telemetry-alerts-dev')
logger = logging.getLogger(__name__)

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador inválido')
    return value

def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('Número inválido')
    result = Decimal(str(value))
    if not result.is_finite() or (low is not None and result < Decimal(str(low))) or (high is not None and result > Decimal(str(high))):
        raise ValueError('Número fuera de rango')
    return result

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def measurement(data):
    antenna = identifier(data['antenna_id'])
    value = number(data['traffic_gb_min'], 0)
    digits = list(value.as_tuple().digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
    if len(digits) > 38 or (value > 0 and (value < Decimal('1e-130') or value.adjusted() > 125)):
        raise ValueError('Precisión numérica no representable en DynamoDB')
    value = value.normalize(Context(prec=38)) if value else Decimal(0)
    date = timestamp(data['timestamp'])
    event_id = 'EVT-' + hashlib.sha256(f'{antenna}|{date}|{value}'.encode()).hexdigest()
    return {'antenna_id': antenna, 'timestamp': date, 'traffic_gb_min': value, 'event_id': event_id}

def classify(value):
    value = number(value, 0)
    return 'DISABLED' if value == 0 else 'DOWN' if value < 60 else 'NORMAL' if value <= 129 else 'SATURATED'

def dumps(value):
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('Non-finite decimal')
        return str(value)
    if isinstance(value, dict):
        if any((not isinstance(key, str) for key in value)):
            raise TypeError('JSON object keys must be strings')
        return '{' + ','.join((json.dumps(key) + ':' + dumps(item) for key, item in value.items())) + '}'
    if isinstance(value, (list, tuple)):
        return '[' + ','.join((dumps(item) for item in value)) + ']'
    return json.dumps(value, allow_nan=False, separators=(',', ':'))

def decimalize(value):
    return json.loads(dumps(value), parse_float=Decimal)

def attributes(item):
    return {k: serializer.serialize(v) for k, v in decimalize(item).items()}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def process(raw):
    item = measurement(raw)
    item['status'] = classify(item['traffic_gb_min'])
    item['measurement_key'] = item['timestamp'] + '#' + item['event_id']
    history = dynamodb.Table(HISTORY)
    latest = dynamodb.Table(LATEST)
    history_key = {k: item[k] for k in ('antenna_id', 'measurement_key')}
    for attempt in range(5):
        if history.get_item(Key=history_key, ConsistentRead=True).get('Item'):
            return
        previous = latest.get_item(Key={'antenna_id': item['antenna_id']}, ConsistentRead=True).get('Item')
        writes = [{'Put': {'TableName': HISTORY, 'Item': attributes(item), 'ConditionExpression': 'attribute_not_exists(measurement_key)'}}]
        if previous is None or item['measurement_key'] > previous['measurement_key']:
            current = dict(item, updated_at=now())
            put = {'TableName': LATEST, 'Item': attributes(current), 'ConditionExpression': 'attribute_not_exists(antenna_id)' if previous is None else 'event_id = :old'}
            if previous:
                put['ExpressionAttributeValues'] = attributes({':old': previous['event_id']})
            writes.append({'Put': put})
            old_status = previous['status'] if previous else None
            if item['status'] != old_status:
                alert = dict(item, alert_timestamp_id=item['measurement_key'], previous_status=old_status, severity='INFO' if item['status'] == 'NORMAL' else 'HIGH', created_at=now(), feed='ALL')
                writes.append({'Put': {'TableName': ALERTS, 'Item': attributes(alert)}})
        try:
            client.transact_write_items(TransactItems=writes)
            return
        except ClientError as exc:
            if exc.response['Error']['Code'] not in ('TransactionCanceledException', 'TransactionConflictException'):
                raise
    raise RuntimeError('Concurrent update retries exhausted')

def lambda_handler(event, context):
    failures = []
    for record in event.get('Records', []):
        try:
            process(json.loads(record['body'], parse_float=Decimal))
        except Exception as exc:
            logger.error('telemetry_retry type=%s', type(exc).__name__)
            failures.append({'itemIdentifier': record['messageId']})
    return {'batchItemFailures': failures}
