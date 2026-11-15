"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import json
import os
import logging
import boto3
import re
logger = logging.getLogger(__name__)
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('LATEST_TABLE', 'rtc-telemetry-latest-dev'))
HEADERS = {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', 'https://localhost'), 'Vary': 'Origin', 'Cache-Control': 'no-store'}

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador inválido')
    return value

def path(event, name):
    return identifier((event.get('pathParameters') or {}).get(name))

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
        item = table.get_item(Key={'antenna_id': path(event, 'antennaId')}, ConsistentRead=True).get('Item')
        if not item:
            return {
                'statusCode': 404,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Antena sin mediciones'}, default=str),
            }
        if len(json.dumps(item, default=str).encode('utf-8')) > 5000000:
            return {
                'statusCode': 413,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Respuesta demasiado grande; reduzca limit o el intervalo'}, default=str),
            }
        return {
            'statusCode': 200,
            'headers': HEADERS,
            'body': json.dumps(item, default=str),
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
