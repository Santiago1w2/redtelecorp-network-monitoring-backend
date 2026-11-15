"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import boto3
from boto3.dynamodb.types import TypeDeserializer
import os
from decimal import Decimal
import json
events = boto3.client('events')
deserializer = TypeDeserializer()
BUS = os.environ.get('EVENT_BUS_NAME', 'rtc-events-dev')

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

def lambda_handler(event, context):
    for record in event.get('Records', []):
        if record['eventName'] != 'INSERT':
            continue
        item = {k: deserializer.deserialize(v) for k, v in record['dynamodb']['NewImage'].items()}
        types = ['telemetry.status.changed']
        if item['status'] in ('DOWN', 'DISABLED', 'SATURATED'):
            types.append('telemetry.threshold.exceeded')
        entries = []
        for event_type in types:
            envelope = {'event_id': item['event_id'], 'event_type': event_type, 'source': 'network-telemetry', 'occurred_at': item['timestamp'], 'version': '1.0', 'data': {'antenna_id': item['antenna_id'], 'previous_status': item['previous_status'], 'current_status': item['status'], 'traffic_gb_min': item['traffic_gb_min']}}
            entries.append({'EventBusName': BUS, 'Source': 'network-telemetry', 'DetailType': event_type, 'Detail': dumps(envelope)})
        result = events.put_events(Entries=entries)
        if result.get('FailedEntryCount', 0):
            raise RuntimeError('EventBridge partial failure')
    return {'published': True}
