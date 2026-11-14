"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import functools
from decimal import Decimal
import json
import os
import logging
from datetime import datetime, timezone
import base64
from boto3.dynamodb.conditions import Key
import boto3
import re
logger = logging.getLogger(__name__)
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('HISTORY_TABLE', 'rtc-telemetry-history-index-dev'))

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

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise Problem(400, 'Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise Problem(400, 'El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def window(query):
    start = timestamp(query.get('from', '1970-01-01T00:00:00Z'))
    end = timestamp(query.get('to', '9999-12-31T23:59:59.999999Z'))
    if start > end:
        raise Problem(400, 'Intervalo invertido')
    return (start, end)

def decode_cursor(token, binding):
    if not token:
        return None
    try:
        if len(token) > 10000:
            raise ValueError()
        data = json.loads(base64.urlsafe_b64decode(token))
        if data['binding'] != binding or not isinstance(data['key'], dict):
            raise ValueError()
        return data['key']
    except Exception:
        raise Problem(400, 'Cursor inválido para esta consulta')

def limit(value, maximum=100):
    result = int(value or 50)
    if not 1 <= result <= maximum:
        raise Problem(400, 'Límite fuera de rango')
    return result

def encode_cursor(key, binding):
    if not key:
        return None
    return base64.urlsafe_b64encode(dumps({'key': key, 'binding': binding}).encode()).decode()

def history_page(table, antenna, query):
    start, end = window(query)
    binding = [antenna, start, end]
    options = {'KeyConditionExpression': Key('antenna_id').eq(antenna) & Key('measurement_key').between(start, end + '~'), 'Limit': limit(query.get('limit')), 'ScanIndexForward': False, 'ConsistentRead': True}
    key = decode_cursor(query.get('cursor'), binding)
    if key:
        if key.get('antenna_id') != antenna:
            raise Problem(400, 'Cursor inválido')
        options['ExclusiveStartKey'] = key
    result = table.query(**options)
    return (result.get('Items', []), encode_cursor(result.get('LastEvaluatedKey'), binding))

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise Problem(400, 'Identificador inválido')
    return value

def path(event, name):
    return identifier((event.get('pathParameters') or {}).get(name))

def params(event):
    return event.get('queryStringParameters') or {}

@http
def lambda_handler(event, context):
    principal(event, 'telemetry:read')
    items, cursor = history_page(table, path(event, 'antennaId'), params(event))
    return response(200, {'measurements': items, 'cursor': cursor})
