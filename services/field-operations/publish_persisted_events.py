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
        event_type = 'work_order.status.changed'
        if record['eventName'] == 'INSERT':
            event_type = 'work_order.pending' if item['status'] == 'PENDING_ASSIGNMENT' else 'work_order.assigned'
        data = {'work_order_id': item['workOrderId'], 'incident_id': item['incidentId'], 'zone_id': item['zoneId'], 'status': item['status']}
        if item.get('assignedTechnicianId'):
            data['assigned_technician_id'] = item['assignedTechnicianId']
        occurred = item.get('updatedAt', item['createdAt'])
        envelope = {'event_id': 'EVT-' + hashlib.sha256(record['eventID'].encode()).hexdigest(),
                    'source': 'field-operations', 'event_type': event_type, 'version': '1.0', 'occurred_at': occurred, 'data': data}
        result = events.put_events(Entries=[{'EventBusName': os.environ['SHARED_EVENT_BUS'], 'Source': envelope['source'],
            'DetailType': event_type, 'Detail': json.dumps(envelope, default=lambda value: float(value) if isinstance(value, Decimal) else str(value))}])
        if result.get('FailedEntryCount'):
            raise RuntimeError('EventBridge rechazo el evento persistido')
    return {'published': True}
