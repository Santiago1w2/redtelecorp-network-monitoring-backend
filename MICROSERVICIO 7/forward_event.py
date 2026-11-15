"""Mantiene el bus de Reports y reenvía sus contratos al bus común de Red TeleCorp."""
import hashlib
import json
import os

import boto3

events = boto3.client('events')
BUS = os.environ['SHARED_EVENT_BUS']
TYPES = {'CustomerReportCreated': 'report.created', 'CustomerReportStatusChanged': 'report.status.changed'}
FIELDS = {'reportId': 'report_id', 'userId': 'user_id', 'zoneId': 'zone_id', 'problemType': 'problem_type',
          'reportedAt': 'reported_at', 'updatedAt': 'updated_at', 'previousStatus': 'previous_status', 'status': 'status'}


def lambda_handler(event, context):
    if event.get('source') != 'redtelecorp.customer-reports' or event.get('detail-type') not in TYPES:
        raise ValueError('Evento de Reports no permitido')
    detail = event['detail']
    envelope = {
        'event_id': 'RPT-' + hashlib.sha256(event['id'].encode()).hexdigest(),
        'event_type': TYPES[event['detail-type']], 'source': 'customer-reports',
        'occurred_at': event['time'], 'version': '1.0',
        'data': {target: detail[key] for key, target in FIELDS.items() if key in detail},
    }
    result = events.put_events(Entries=[{'EventBusName': BUS, 'Source': envelope['source'],
        'DetailType': envelope['event_type'], 'Detail': json.dumps(envelope)}])
    if result.get('FailedEntryCount'):
        raise RuntimeError('EventBridge rechazo el evento de Reports')
    return {'forwarded': envelope['event_id']}
