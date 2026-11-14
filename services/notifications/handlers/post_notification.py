"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import functools
from decimal import Decimal
import json
import os
import logging
import re
from botocore.awsrequest import AWSRequest
from urllib import error, request
from botocore.auth import SigV4Auth
import boto3
from botocore.exceptions import ClientError
from datetime import datetime, timezone
import uuid
import hashlib
from boto3.dynamodb.types import TypeSerializer
import time
import base64
logger = logging.getLogger(__name__)
TEMPLATES = {'INCIDENT_DETECTED': ('Incidente en evaluación', 'Estamos evaluando el incidente en {zone_name}. Estado: {incident_status}.'), 'INCIDENT_IN_PROGRESS': ('Incidente en atención', 'Nuestro equipo está atendiendo el incidente en {zone_name}. Estado: {incident_status}.'), 'INCIDENT_RESOLVED': ('Servicio restablecido', 'El incidente en {zone_name} fue resuelto. Estado: {incident_status}.')}
IDENTITY_URL = os.environ.get('IDENTITY_API_URL', '')
IDEMPOTENCY = os.environ.get('IDEMPOTENCY_TABLE', 'rtc-notification-idempotency-dev')
dynamodb = boto3.resource('dynamodb')
client = boto3.client('dynamodb')
serializer = TypeSerializer()
NOTIFICATIONS = os.environ.get('NOTIFICATIONS_TABLE', 'rtc-notifications-dev')

class Problem(Exception):

    def __init__(self, status, message):
        self.status, self.message = (status, message)

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

def response(status, data):
    payload = dumps(data)
    if len(payload.encode()) > 5000000:
        status, payload = (413, dumps({'error': 'Respuesta demasiado grande; reduzca limit o el intervalo'}))
    return {'statusCode': status, 'headers': {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', 'https://localhost'), 'Vary': 'Origin', 'Cache-Control': 'no-store'}, 'body': payload}

def http(fn):

    @functools.wraps(fn)
    def wrapped(event, context):
        try:
            return fn(event, context)
        except Problem as exc:
            return response(exc.status, {'error': exc.message})
        except (ValueError, TypeError, KeyError, OverflowError):
            return response(400, {'error': 'Entrada inválida'})
        except Exception as exc:
            logger.error('operation_failed type=%s', type(exc).__name__)
            return response(503, {'error': 'Dependencia temporalmente no disponible'})
    return wrapped

def principal(event, scope=None):
    auth = event.get('requestContext', {}).get('authorizer', {})
    claims = auth.get('jwt', {}).get('claims', auth.get('claims', auth))
    user = claims.get('sub')
    if not user:
        raise Problem(401, 'Autenticación requerida')
    scopes = str(claims.get('scope', '')).split()
    if scope and scope not in scopes:
        raise Problem(403, 'Permiso insuficiente')
    return user

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise Problem(400, 'Identificador inválido')
    return value

def validate(data):
    identifier(data['recipient_id'])
    identifier(data['incident_id'])
    if data['template'] not in TEMPLATES:
        raise Problem(400, 'Plantilla inválida')
    channels = data['channels']
    if not isinstance(channels, list) or not channels or len(channels) > 3 or (len(set(channels)) != len(channels)) or set(channels) - {'EMAIL', 'SMS', 'IN_APP'}:
        raise Problem(400, 'Canales inválidos')
    variables = data.get('variables', {})
    if not isinstance(variables, dict) or set(variables) != {'zone_name', 'incident_status'} or any((not isinstance(v, str) or not 1 <= len(v) <= 200 for v in variables.values())):
        raise Problem(400, 'Variables requeridas: zone_name e incident_status')
    return {k: data[k] for k in ('recipient_id', 'incident_id', 'template', 'channels', 'variables')}

def external_json(base_url, route, method='GET', payload=None):
    """API privada de otro equipo con AWS_IAM; TLS y tiempo límite explícito."""
    if not base_url:
        raise Problem(503, 'API de integración sin configurar')
    url = base_url.rstrip('/') + route
    if not url.startswith('https://') and (not (os.environ.get('LOCAL_TEST') == 'true' and url.startswith('http://127.0.0.1:'))):
        raise Problem(503, 'La integración requiere HTTPS')
    data = dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    if not os.environ.get('LOCAL_TEST') == 'true':
        credentials = boto3.Session().get_credentials().get_frozen_credentials()
        signed = AWSRequest(method=method, url=url, data=data, headers=headers)
        SigV4Auth(credentials, 'execute-api', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(signed)
        headers = dict(signed.headers.items())
    try:
        with request.urlopen(request.Request(url, data=data, headers=headers, method=method), timeout=5) as result:
            return json.loads(result.read(1000000), parse_float=Decimal)
    except error.HTTPError as exc:
        if exc.code == 404:
            raise Problem(404, 'Recurso externo no encontrado')
        raise Problem(503, 'API externa no disponible')
    except (error.URLError, TimeoutError):
        raise Problem(503, 'API externa no disponible')

def recipient(user):
    result = external_json(IDENTITY_URL, '/v1/identity/users/' + user + '/notification-contact')
    if result.get('user_id') != user or not result.get('active'):
        raise Problem(404, 'Destinatario inexistente/inactivo')
    return result

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise Problem(400, 'Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise Problem(400, 'El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

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
            raise Problem(409, 'Idempotency-Key reutilizada con otro contenido')
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
            raise Problem(409, 'Idempotency-Key reutilizada con otro contenido')
        return existing['notification_id']
    return notification_id

def body(event):
    raw = event.get('body', '{}')
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw, validate=True).decode()
    value = json.loads(raw, parse_float=Decimal) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise Problem(400, 'Se requiere un objeto JSON')
    if len(dumps(value).encode()) > 200000:
        raise Problem(413, 'Solicitud demasiado grande')
    return value

@http
def lambda_handler(event, context):
    sender = principal(event, 'notifications:send')
    data = validate(body(event))
    recipient(data['recipient_id'])
    headers = {k.lower(): v for k, v in (event.get('headers') or {}).items()}
    key = identifier(headers.get('idempotency-key', str(uuid.uuid4())))
    notification_id = create_notification(data, 'http:' + sender + ':' + key)
    return response(202, {'notification_id': notification_id, 'status': 'QUEUED'})
