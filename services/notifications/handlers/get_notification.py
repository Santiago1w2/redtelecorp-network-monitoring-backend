"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import functools
from decimal import Decimal
import json
import os
import logging
import boto3
import re
logger = logging.getLogger(__name__)
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('NOTIFICATIONS_TABLE', 'rtc-notifications-dev'))

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

def owned_notification(table, notification_id, user):
    from boto3.dynamodb.conditions import Key
    result = table.query(IndexName='notification-id', KeyConditionExpression=Key('notification_id').eq(notification_id), Limit=1)
    items = result.get('Items', [])
    if not items or items[0]['recipient_id'] != user:
        raise Problem(404, 'Notificación no encontrada')
    key = {k: items[0][k] for k in ('recipient_id', 'notification_key')}
    item = table.get_item(Key=key, ConsistentRead=True).get('Item')
    if not item or item['recipient_id'] != user:
        raise Problem(404, 'Notificación no encontrada')
    return item

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise Problem(400, 'Identificador inválido')
    return value

def path(event, name):
    return identifier((event.get('pathParameters') or {}).get(name))

@http
def lambda_handler(event, context):
    user = principal(event)
    return response(200, owned_notification(table, path(event, 'notificationId'), user))
