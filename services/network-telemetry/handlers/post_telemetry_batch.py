"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
from decimal import Decimal, Context
import json
import os
import logging
import base64
import re
from datetime import datetime, timezone
import hashlib
from botocore.awsrequest import AWSRequest
from urllib import error, request
from botocore.auth import SigV4Auth
import boto3
logger = logging.getLogger(__name__)
MAX_BATCH = int(os.environ.get('MAX_BATCH', '100'))
sqs = boto3.client('sqs')
QUEUE_URL = os.environ.get('INGEST_QUEUE_URL', '')
HEADERS = {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', 'https://localhost'), 'Vary': 'Origin', 'Cache-Control': 'no-store'}

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

def body(event):
    raw = event.get('body', '{}')
    if event.get('isBase64Encoded'):
        raw = base64.b64decode(raw, validate=True).decode()
    value = json.loads(raw, parse_float=Decimal) if isinstance(raw, str) else raw
    if not isinstance(value, dict):
        raise ValueError('Se requiere un objeto JSON')
    return value

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador inválido')
    return value

def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('Número inválido')
    result = Decimal(str(value))
    if not result.is_finite() or (low is not None and result < Decimal(str(low))) or (high is not None and result > Decimal(str(high))):
        raise ValueError('Número fuera de rango')
    return result

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def measurement(data):
    antenna = identifier(data['antenna_id'])
    value = number(data['traffic_gb_min'], 0)
    digits = list(value.as_tuple().digits)
    while len(digits) > 1 and digits[-1] == 0:
        digits.pop()
    if len(digits) > 38 or (value > 0 and (value < Decimal('1e-130') or value.adjusted() > 125)):
        raise ValueError('Precisión numérica no representable en DynamoDB')
    value = value.normalize(Context(prec=38)) if value else Decimal(0)
    date = timestamp(data['timestamp'])
    event_id = 'EVT-' + hashlib.sha256(f'{antenna}|{date}|{value}'.encode()).hexdigest()
    return {'antenna_id': antenna, 'timestamp': date, 'traffic_gb_min': value, 'event_id': event_id}

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

def validate_antenna(antenna):
    result = external_json(os.environ.get('INFRASTRUCTURE_API_URL'), '/v1/infrastructure/antennas/' + antenna)
    if result.get('antenna_id') != antenna:
        raise LookupError('Antena inexistente')
    return result

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
        if not groups.intersection({'admin', 'supervisor_red'}):
            return {
                'statusCode': 403,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Permiso insuficiente'}, default=str),
            }
        request_data = body(event)
        if len(dumps(request_data).encode('utf-8')) > 200000:
            return {
                'statusCode': 413,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Solicitud demasiado grande'}, default=str),
            }
        entries = request_data.get('measurements')
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_BATCH:
            return {
                'statusCode': 400,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Lote vacío o sobre el límite'}, default=str),
            }
        accepted, failed, pending, checked = ([], [], [], set())
        for index, raw in enumerate(entries):
            try:
                item = measurement(raw)
                if item['antenna_id'] not in checked:
                    validate_antenna(item['antenna_id'])
                    checked.add(item['antenna_id'])
                pending.append((index, item))
            except (ValueError, TypeError, KeyError, LookupError, ConnectionError) as exc:
                code = 503 if isinstance(exc, ConnectionError) else 404 if isinstance(exc, LookupError) and (not isinstance(exc, KeyError)) else 400
                failed.append({'index': index, 'code': code, 'retryable': code >= 500})
        for offset in range(0, len(pending), 10):
            chunk = pending[offset:offset + 10]
            try:
                result = sqs.send_message_batch(QueueUrl=QUEUE_URL, Entries=[{'Id': str(i), 'MessageBody': dumps(item)} for i, item in chunk])
                success = {x['Id'] for x in result.get('Successful', [])}
                for i, item in chunk:
                    (accepted if str(i) in success else failed).append({'index': i, 'event_id': item['event_id']} if str(i) in success else {'index': i, 'event_id': item['event_id'], 'code': 'SQS_FAILED', 'retryable': True})
            except Exception:
                failed.extend(({'index': i, 'event_id': item['event_id'], 'code': 'SQS_UNAVAILABLE', 'retryable': True} for i, item in chunk))
        result = {'accepted': accepted, 'failed': failed}
        if len(json.dumps(result, default=str).encode('utf-8')) > 5000000:
            return {
                'statusCode': 413,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Respuesta demasiado grande; reduzca limit o el intervalo'}, default=str),
            }
        return {
            'statusCode': 207 if failed else 202,
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
    except LookupError as exc:
        return {
            'statusCode': 404,
            'headers': HEADERS,
            'body': json.dumps({'message': str(exc)}, default=str),
        }
    except ConnectionError as exc:
        return {
            'statusCode': 503,
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
