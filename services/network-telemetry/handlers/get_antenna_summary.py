"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import functools
from decimal import Decimal
import json
import os
import logging
import re
from datetime import datetime, timezone
import boto3
from boto3.dynamodb.conditions import Key
logger = logging.getLogger(__name__)
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('HISTORY_TABLE', 'rtc-telemetry-history-index-dev'))
MAX_SUMMARY = int(os.environ.get('MAX_SUMMARY', '20000'))

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

def path(event, name):
    return identifier((event.get('pathParameters') or {}).get(name))

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

def params(event):
    return event.get('queryStringParameters') or {}

@http
def lambda_handler(event, context):
    principal(event, 'telemetry:read')
    antenna = path(event, 'antennaId')
    start, end = window(params(event))
    options = {'KeyConditionExpression': Key('antenna_id').eq(antenna) & Key('measurement_key').between(start, end + '~'), 'ConsistentRead': True}
    count, total, minimum, maximum = (0, Decimal(0), None, None)
    states = dict.fromkeys(['DISABLED', 'DOWN', 'NORMAL', 'SATURATED'], 0)
    while True:
        page = table.query(**options)
        for item in page.get('Items', []):
            count += 1
            if count > MAX_SUMMARY:
                raise Problem(422, 'Intervalo demasiado amplio; reduzca from/to')
            value = item['traffic_gb_min']
            total += value
            minimum = value if minimum is None else min(minimum, value)
            maximum = value if maximum is None else max(maximum, value)
            states[item['status']] += 1
        if not page.get('LastEvaluatedKey'):
            break
        options['ExclusiveStartKey'] = page['LastEvaluatedKey']
    return response(200, {'antenna_id': antenna, 'count': count, 'average': total / count if count else None, 'minimum': minimum, 'maximum': maximum, 'by_status': states})
