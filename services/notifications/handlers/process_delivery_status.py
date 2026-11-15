"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import re
from datetime import datetime, timezone
from botocore.exceptions import ClientError
import boto3
import os
from boto3.dynamodb.conditions import Key
import json
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('NOTIFICATIONS_TABLE', 'rtc-notifications-dev'))
TOPIC = os.environ.get('DELIVERY_TOPIC_ARN', '')

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador inválido')
    return value

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def event_envelope(event, source, types):
    detail = event.get('detail', {})
    if event.get('source') != source or event.get('detail-type') not in types:
        raise ValueError('Origen de evento inválido')
    if detail.get('version') != '1.0' or detail.get('source') != source or detail.get('event_type') != event['detail-type']:
        raise ValueError('Contrato de evento inválido')
    identifier(detail['event_id'])
    timestamp(detail['occurred_at'])
    return detail

def apply_status(data):
    channel, status = (data['channel'], data['status'])
    if channel not in ('EMAIL', 'SMS') or status not in ('DELIVERED', 'BOUNCED', 'COMPLAINT', 'FAILED'):
        raise ValueError('Estado de entrega inválido')
    date = timestamp(data['occurred_at'])
    page = table.query(IndexName='notification-id', KeyConditionExpression=Key('notification_id').eq(data['notification_id']), Limit=1)
    if not page.get('Items'):
        raise RuntimeError('Notification index not visible yet')
    item = page['Items'][0]
    key = {k: item[k] for k in ('recipient_id', 'notification_key')}
    current = table.get_item(Key=key, ConsistentRead=True)['Item']['delivery_status'].get(channel, {})
    if current.get('provider_message_id') != data['provider_message_id']:
        raise RuntimeError('Provider message does not match; retry/reconcile')
    if current.get('callback_at', '') >= date:
        return
    if status == 'DELIVERED' and current.get('status') in ('BOUNCED', 'COMPLAINT', 'FAILED'):
        return
    try:
        table.update_item(Key=key, UpdateExpression='SET #d.#c.#s = :s, #d.#c.callback_at = :t', ConditionExpression='#d.#c.provider_message_id = :m AND (attribute_not_exists(#d.#c.callback_at) OR #d.#c.callback_at < :t)', ExpressionAttributeNames={'#d': 'delivery_status', '#c': channel, '#s': 'status'}, ExpressionAttributeValues={':s': status, ':t': date, ':m': data['provider_message_id']})
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise

def lambda_handler(event, context):
    if 'Records' in event:
        for record in event['Records']:
            if record.get('EventSource') != 'aws:sns' or record['Sns']['TopicArn'] != TOPIC:
                raise PermissionError('Topic no autorizado')
            payload = json.loads(record['Sns']['Message'])
            kinds = {'Delivery': 'DELIVERED', 'Bounce': 'BOUNCED', 'Complaint': 'COMPLAINT', 'Reject': 'FAILED'}
            if payload.get('eventType') not in kinds:
                continue
            apply_status({'channel': 'EMAIL', 'status': kinds[payload['eventType']], 'notification_id': payload['mail']['tags']['notification_id'][0], 'provider_message_id': payload['mail']['messageId'], 'occurred_at': payload.get(payload['eventType'].lower(), {}).get('timestamp', payload['mail']['timestamp'])})
    else:
        detail = event_envelope(event, 'notification-provider', ['notification.delivery.updated'])
        apply_status(dict(detail['data'], occurred_at=detail['occurred_at']))
    return {'updated': True}
