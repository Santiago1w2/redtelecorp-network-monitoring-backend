"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import re
from datetime import datetime, timezone
from botocore.awsrequest import AWSRequest
from urllib import error, request
from decimal import Decimal
import json
from botocore.auth import SigV4Auth
import os
import boto3
from botocore.exceptions import ClientError
import uuid
import hashlib
from boto3.dynamodb.types import TypeSerializer
import time
from urllib.parse import quote
TYPES = {'incident.collective.detected': 'INCIDENT_DETECTED', 'incident.status.changed': 'INCIDENT_IN_PROGRESS', 'incident.resolved': 'INCIDENT_RESOLVED'}
REPORTS_URL = os.environ.get('REPORTS_API_URL', '')
TEMPLATES = {'INCIDENT_DETECTED': ('Incidente en evaluación', 'Estamos evaluando el incidente en {zone_name}. Estado: {incident_status}.'), 'INCIDENT_IN_PROGRESS': ('Incidente en atención', 'Nuestro equipo está atendiendo el incidente en {zone_name}. Estado: {incident_status}.'), 'INCIDENT_RESOLVED': ('Servicio restablecido', 'El incidente en {zone_name} fue resuelto. Estado: {incident_status}.')}
IDEMPOTENCY = os.environ.get('IDEMPOTENCY_TABLE', 'rtc-notification-idempotency-dev')
dynamodb = boto3.resource('dynamodb')
client = boto3.client('dynamodb')
serializer = TypeSerializer()
NOTIFICATIONS = os.environ.get('NOTIFICATIONS_TABLE', 'rtc-notifications-dev')

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

def external_json(base_url, route, method='GET', payload=None):
    """API privada de otro equipo con AWS_IAM; TLS y tiempo límite explícito."""
    if not base_url:
        raise ConnectionError('API de integración sin configurar')
    url = base_url.rstrip('/') + route
    if not url.startswith('https://') and (not (os.environ.get('LOCAL_TEST') == 'true' and url.startswith('http://127.0.0.1:'))):
        raise ConnectionError('La integración requiere HTTPS')
    data = dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    signed = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(credentials, 'execute-api', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(signed)
    headers = dict(signed.headers.items())
    try:
        with request.urlopen(request.Request(url, data=data, headers=headers, method=method), timeout=5) as result:
            return json.loads(result.read(1000000), parse_float=Decimal)
    except error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError('Recurso externo no encontrado')
        raise ConnectionError('API externa no disponible')
    except (error.URLError, TimeoutError):
        raise ConnectionError('API externa no disponible')

def validate(data):
    identifier(data['recipient_id'])
    identifier(data['incident_id'])
    if data['template'] not in TEMPLATES:
        raise ValueError('Plantilla inválida')
    channels = data['channels']
    if not isinstance(channels, list) or not channels or len(channels) > 3 or (len(set(channels)) != len(channels)) or set(channels) - {'EMAIL', 'SMS', 'IN_APP'}:
        raise ValueError('Canales inválidos')
    variables = data.get('variables', {})
    if not isinstance(variables, dict) or set(variables) != {'zone_name', 'incident_status'} or any((not isinstance(v, str) or not 1 <= len(v) <= 200 for v in variables.values())):
        raise ValueError('Variables requeridas: zone_name e incident_status')
    return {k: data[k] for k in ('recipient_id', 'incident_id', 'template', 'channels', 'variables')}

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def decimalize(value):
    return json.loads(dumps(value), parse_float=Decimal)

def create_notification(data, idempotency_key, occurred_at=None):
    data = validate(data)
    digest = hashlib.sha256(dumps({**data, 'channels': sorted(data['channels']), 'variables': dict(sorted(data['variables'].items()))}).encode()).hexdigest()
    dedup = dynamodb.Table(IDEMPOTENCY)
    existing = dedup.get_item(Key={'idempotency_key': idempotency_key}, ConsistentRead=True).get('Item')
    if existing:
        if existing['payload_hash'] != digest:
            return None
        return existing['notification_id']
    notification_id = str(uuid.uuid4())
    created = timestamp(occurred_at) if occurred_at else now()
    title, message = TEMPLATES[data['template']]
    item = dict(data, notification_id=notification_id, notification_key=created + '#' + notification_id, created_at=created, title=title, message=message.format(**data['variables']), read=False, delivery_status={channel: {'status': 'PENDING', 'attempts': 0} for channel in data['channels']})
    entry = {'idempotency_key': idempotency_key, 'status': 'CREATED', 'created_at': created, 'notification_id': notification_id, 'payload_hash': digest, 'expires_at': int(time.time()) + 90 * 86400}

    def attrs(value):
        return {k: serializer.serialize(v) for k, v in decimalize(value).items()}
    try:
        client.transact_write_items(TransactItems=[{'Put': {'TableName': IDEMPOTENCY, 'Item': attrs(entry), 'ConditionExpression': 'attribute_not_exists(idempotency_key)'}}, {'Put': {'TableName': NOTIFICATIONS, 'Item': attrs(item), 'ConditionExpression': 'attribute_not_exists(notification_key)'}}])
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'TransactionCanceledException':
            raise
        existing = dedup.get_item(Key={'idempotency_key': idempotency_key}, ConsistentRead=True).get('Item')
        if not existing:
            raise
        if existing['payload_hash'] != digest:
            return None
        return existing['notification_id']
    return notification_id

def lambda_handler(event, context):
    detail = event_envelope(event, 'incident-management', list(TYPES))
    data = detail['data']
    incident = identifier(data['incident_id'])
    cursor, count, visited = (None, 0, set())
    while True:
        route = '/v1/reports/incidents/' + incident + '/recipients?limit=100'
        if cursor:
            route += '&cursor=' + quote(cursor, safe='')
        page = external_json(REPORTS_URL, route)
        recipients = page['recipient_ids']
        if not isinstance(recipients, list) or len(recipients) > 100:
            raise ConnectionError('Contrato de destinatarios inválido')
        for user in recipients:
            identifier(user)
            targeted = data.get('recipient_ids')
            if targeted is not None:
                if not isinstance(targeted, list) or len(targeted) > 100 or any(not isinstance(value, str) for value in targeted):
                    raise ValueError('Destinatarios dirigidos invalidos')
                if user not in targeted:
                    continue
            notification_id = create_notification({'recipient_id': user, 'incident_id': incident, 'template': TYPES[detail['event_type']], 'channels': ['EMAIL', 'SMS', 'IN_APP'], 'variables': {'zone_name': data['zone_name'], 'incident_status': data['incident_status']}}, 'event:' + detail['event_id'] + ':' + user, detail['occurred_at'])
            if notification_id is None:
                raise ValueError('Evento repetido con contenido diferente')
            count += 1
        cursor = page.get('cursor')
        if not cursor:
            break
        if cursor in visited or len(visited) >= 99:
            raise ConnectionError('Paginación inválida o incidente mayor que 10000 destinatarios')
        visited.add(cursor)
    return {'created_or_deduplicated': count}
