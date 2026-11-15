"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
from decimal import Decimal
import json
import os
import logging
import base64
import re
from shapely.geometry import shape, mapping
import math
from botocore.exceptions import ClientError
import boto3
from datetime import datetime, timezone
import hashlib
logger = logging.getLogger(__name__)
MAX_CELLS = 256
CELL_SIZE = 0.1
s3 = boto3.client('s3')
BUCKET = os.environ.get('GEOSPATIAL_BUCKET', '')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('ZONES_TABLE', 'rtc-geozones-dev'))
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

def cells(bounds):
    a, b, c, d = bounds
    x1, x2 = (math.floor((a + 180) / CELL_SIZE), min(3599, math.floor((c + 180) / CELL_SIZE)))
    y1, y2 = (math.floor((b + 90) / CELL_SIZE), min(1799, math.floor((d + 90) / CELL_SIZE)))
    x1, y1 = (min(x1, 3599), min(y1, 1799))
    if (x2 - x1 + 1) * (y2 - y1 + 1) > MAX_CELLS:
        raise OverflowError('Área demasiado amplia; subdivida el bbox/geometría')
    return [f'{x:04d}:{y:04d}' for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)]

def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('Número inválido')
    result = Decimal(str(value))
    if not result.is_finite() or (low is not None and result < Decimal(str(low))) or (high is not None and result > Decimal(str(high))):
        raise ValueError('Número fuera de rango')
    return result

def geometry(value):
    if not isinstance(value, dict) or value.get('type') not in ('Polygon', 'MultiPolygon'):
        raise ValueError('Se requiere Polygon o MultiPolygon')
    polygons = [value.get('coordinates')] if value['type'] == 'Polygon' else value.get('coordinates')
    if not isinstance(polygons, list) or not polygons:
        raise ValueError('Coordenadas vacías')
    vertices = 0
    try:
        for polygon in polygons:
            if not polygon:
                raise ValueError()
            for ring in polygon:
                if len(ring) < 4 or ring[0] != ring[-1]:
                    raise ValueError()
                for point in ring:
                    if len(point) != 2:
                        raise ValueError()
                    number(point[0], -180, 180)
                    number(point[1], -89, 89)
                    vertices += 1
        if vertices > 5000:
            raise ValueError()
        result = shape(json.loads(dumps(value)))
        if result.is_empty or not result.is_valid or result.area <= 0 or (result.bounds[2] - result.bounds[0] > 180):
            raise ValueError()
        cells(result.bounds)
        return result
    except (ValueError, TypeError, IndexError):
        raise ValueError('Geometría GeoJSON inválida')

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def decimalize(value):
    return json.loads(dumps(value), parse_float=Decimal)

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
        data = request_data
        zone = identifier(data['zone_id'])
        if not isinstance(data['name'], str) or not 1 <= len(data['name'].strip()) <= 200:
            return {
                'statusCode': 400,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Nombre inválido'}, default=str),
            }
        if data['zone_type'] not in ('HEALTHCARE', 'RESIDENTIAL', 'COMMERCIAL', 'INDUSTRIAL', 'EDUCATION', 'OTHER') or data['criticality'] not in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL'):
            return {
                'statusCode': 400,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Tipo/criticidad inválida'}, default=str),
            }
        geom = geometry(data['geometry'])
        content = dumps(mapping(geom)).encode()
        key = f'geospatial/zones/{zone}/{hashlib.sha256(content).hexdigest()}.geojson'
        s3.put_object(Bucket=BUCKET, Key=key, Body=content, ContentType='application/geo+json', ServerSideEncryption='AES256')
        item = {k: data[k] for k in ('zone_id', 'name', 'zone_type', 'criticality')}
        item.update(geometry_s3_key=key, updated_at=now(), index_cells=cells(geom.bounds) + ['ZONES'])
        try:
            table.put_item(Item=decimalize(item), ConditionExpression='attribute_not_exists(zone_id)')
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return {
                    'statusCode': 409,
                    'headers': HEADERS,
                    'body': json.dumps({'message': 'La zona ya existe'}, default=str),
                }
            raise
        if len(json.dumps(item, default=str).encode('utf-8')) > 5000000:
            return {
                'statusCode': 413,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Respuesta demasiado grande; reduzca limit o el intervalo'}, default=str),
            }
        return {
            'statusCode': 201,
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
    except OverflowError as exc:
        return {
            'statusCode': 422,
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
