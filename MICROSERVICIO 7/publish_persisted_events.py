import hashlib
import json
import os
from decimal import Decimal
import boto3
from boto3.dynamodb.types import TypeDeserializer

events = boto3.client('events')
deserialize = TypeDeserializer()

def lambda_handler(event, context):
    for record in event.get('Records', []):
        if record['eventName'] not in ('INSERT', 'MODIFY'):
            continue
        item = {key: deserialize.deserialize(value) for key, value in record['dynamodb']['NewImage'].items()}
        old = {key: deserialize.deserialize(value) for key, value in record['dynamodb'].get('OldImage', {}).items()}

        if record['eventName'] == 'MODIFY' and item.get('status') == old.get('status'):
            continue
        event_type = 'report.created' if record['eventName'] == 'INSERT' else 'report.status.changed'
        data = {'report_id': item['reportId'], 'user_id': item['userId'], 'zone_id': item['zoneId'],
                'description': item['description'], 'problem_type': item['problemType'], 'reported_at': item['reportedAt'], 'status': item['status']}
        occurred = item.get('updatedAt', item['reportedAt'])
        envelope = {'event_id': 'EVT-' + hashlib.sha256(record['eventID'].encode()).hexdigest(),
                    'source': 'customer-reports', 'event_type': event_type, 'version': '1.0', 'occurred_at': occurred, 'data': data}
        result = events.put_events(Entries=[{'EventBusName': os.environ['SHARED_EVENT_BUS'], 'Source': envelope['source'],
            'DetailType': event_type, 'Detail': json.dumps(envelope, default=lambda value: float(value) if isinstance(value, Decimal) else str(value))}])
        if result.get('FailedEntryCount'):
            raise RuntimeError('EventBridge rechazo el evento persistido')
    return {'published': True}
