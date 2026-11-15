"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
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
HEADERS = {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', 'https://localhost'), 'Vary': 'Origin', 'Cache-Control': 'no-store'}

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador inválido')
    return value

def path(event, name):
    return identifier((event.get('pathParameters') or {}).get(name))

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def window(query):
    start = timestamp(query.get('from', '1970-01-01T00:00:00Z'))
    end = timestamp(query.get('to', '9999-12-31T23:59:59.999999Z'))
    if start > end:
        raise ValueError('Intervalo invertido')
    return (start, end)

def params(event):
    return event.get('queryStringParameters') or {}

def lambda_handler(event, context):
    try:
        # API Gateway verifica el token con el User Pool de Identity.
        claims = ((event.get('requestContext') or {}).get('authorizer') or {}).get('claims') or {}
        if not isinstance(claims, dict):
            claims = {}
        user = claims.get('sub')
        if not isinstance(user, str) or not user.strip():
            return {
                'statusCode': 401,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Autenticación requerida'}, default=str),
            }
        # Cognito/API Gateway puede entregar los grupos como lista o texto.
        groups = claims.get('cognito:groups', [])
        if isinstance(groups, str):
            try:
                groups = json.loads(groups)
            except json.JSONDecodeError:
                groups = groups.strip()
                if groups.startswith('[') and groups.endswith(']'):
                    groups = groups[1:-1]
                groups = groups.split(',')
        if not isinstance(groups, list) or any(not isinstance(group, str) for group in groups):
            groups = []
        groups = {group.strip() for group in groups}
        if not groups.intersection({'admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo'}):
            return {
                'statusCode': 403,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Permiso insuficiente'}, default=str),
            }
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
                    return {
                        'statusCode': 422,
                        'headers': HEADERS,
                        'body': json.dumps({'message': 'Intervalo demasiado amplio; reduzca from/to'}, default=str),
                    }
                value = item['traffic_gb_min']
                total += value
                minimum = value if minimum is None else min(minimum, value)
                maximum = value if maximum is None else max(maximum, value)
                states[item['status']] += 1
            if not page.get('LastEvaluatedKey'):
                break
            options['ExclusiveStartKey'] = page['LastEvaluatedKey']
        result = {'antenna_id': antenna, 'count': count, 'average': total / count if count else None, 'minimum': minimum, 'maximum': maximum, 'by_status': states}
        if len(json.dumps(result, default=str).encode('utf-8')) > 5000000:
            return {
                'statusCode': 413,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Respuesta demasiado grande; reduzca limit o el intervalo'}, default=str),
            }
        return {
            'statusCode': 200,
            'headers': HEADERS,
            'body': json.dumps(result, default=str),
        }
    except (KeyError, TypeError) as exc:
        return {
            'statusCode': 400,
            'headers': HEADERS,
            'body': json.dumps({'message': 'Entrada inválida'}, default=str),
        }
    except ValueError as exc:
        return {
            'statusCode': 400,
            'headers': HEADERS,
            'body': json.dumps({'message': str(exc)}, default=str),
        }
    except Exception as exc:
        logger.error('operation_failed type=%s', type(exc).__name__)
        return {
            'statusCode': 503,
            'headers': HEADERS,
            'body': json.dumps({'message': 'Dependencia temporalmente no disponible'}, default=str),
        }
