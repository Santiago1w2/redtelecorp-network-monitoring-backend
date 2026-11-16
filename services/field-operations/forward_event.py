"""Mantiene los eventos de Field Operations en default y los reenvía al bus común."""
import hashlib
import json
import os

import boto3

events = boto3.client('events')
BUS = os.environ['SHARED_EVENT_BUS']
TYPES = {'WorkOrderAssigned': 'work_order.assigned', 'WorkOrderPending': 'work_order.pending',
         'WorkOrderStatusChanged': 'work_order.status.changed'}
FIELDS = {'workOrderId': 'work_order_id', 'incidentId': 'incident_id', 'zoneId': 'zone_id',
          'status': 'status', 'assignedTechnicianId': 'assigned_technician_id'}


def lambda_handler(event, context):
    if event.get('source') != 'redtelecorp.field-operations' or event.get('detail-type') not in TYPES:
        raise ValueError('Evento de Field Operations no permitido')
    detail = event['detail']
    envelope = {
        'event_id': 'WO-' + hashlib.sha256(event['id'].encode()).hexdigest(),
        'event_type': TYPES[event['detail-type']], 'source': 'field-operations',
        'occurred_at': event['time'], 'version': '1.0',
        'data': {target: detail[key] for key, target in FIELDS.items() if key in detail},
    }
    result = events.put_events(Entries=[{'EventBusName': BUS, 'Source': envelope['source'],
        'DetailType': envelope['event_type'], 'Detail': json.dumps(envelope)}])
    if result.get('FailedEntryCount'):
        raise RuntimeError('EventBridge rechazo el evento de Field Operations')
    return {'forwarded': envelope['event_id']}
