"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
from decimal import Decimal
import json
import os
import logging
import math
import base64
from shapely.geometry import shape, mapping, box
from boto3.dynamodb.conditions import Key
import boto3
logger = logging.getLogger(__name__)
MAX_CELLS = 256
CELL_SIZE = 0.1
dynamodb = boto3.resource('dynamodb')
index = dynamodb.Table(os.environ.get('SPATIAL_INDEX_TABLE', 'rtc-spatial-index-dev'))
table = dynamodb.Table(os.environ.get('COVERAGE_TABLE', 'rtc-coverage-dev'))
s3 = boto3.client('s3')
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

def cells(bounds):
    a, b, c, d = bounds
    x1, x2 = (math.floor((a + 180) / CELL_SIZE), min(3599, math.floor((c + 180) / CELL_SIZE)))
    y1, y2 = (math.floor((b + 90) / CELL_SIZE), min(1799, math.floor((d + 90) / CELL_SIZE)))
    x1, y1 = (min(x1, 3599), min(y1, 1799))
    if (x2 - x1 + 1) * (y2 - y1 + 1) > MAX_CELLS:
        raise OverflowError('Área demasiado amplia; subdivida el bbox/geometría')
    return [f'{x:04d}:{y:04d}' for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)]

def bbox(value):
    try:
        values = [float(x) for x in value.split(',')]
        if len(values) != 4:
            raise ValueError()
        a, b, c, d = values
        if not all((math.isfinite(x) for x in values)) or not (-180 <= a < c <= 180 and -90 <= b < d <= 90):
            raise ValueError()
        cells(values)
        return values
    except (ValueError, TypeError, AttributeError):
        raise ValueError('bbox inválido: minLon,minLat,maxLon,maxLat')

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
        raise ValueError('Cursor inválido para esta consulta')

def limit(value, maximum=100):
    result = int(value or 50)
    if not 1 <= result <= maximum:
        raise ValueError('Límite fuera de rango')
    return result

def encode_cursor(key, binding):
    if not key:
        return None
    return base64.urlsafe_b64encode(dumps({'key': key, 'binding': binding}).encode()).decode()

def load_geometry(s3, item):
    return shape(json.loads(s3.get_object(Bucket=os.environ['GEOSPATIAL_BUCKET'], Key=item['geometry_s3_key'])['Body'].read()))

def spatial_page(index, table, s3, kind, query):
    bounds = bbox(query['bbox']) if query.get('bbox') else None
    if kind == 'ANTENNA' and bounds is None:
        raise ValueError('bbox requerido')
    selected = cells(bounds) if bounds else ['ZONES']
    binding = [kind, bounds, query.get('zone_type'), query.get('criticality')]
    cursor = decode_cursor(query.get('cursor'), binding) or {'cell': 0}
    position = int(cursor.get('cell', 0))
    if not 0 <= position < len(selected):
        raise ValueError('Cursor fuera de rango')
    maximum, features, examined = (limit(query.get('limit')), [], 0)
    while position < len(selected) and len(features) < maximum and (examined < 500):
        cell = selected[position]
        options = {'KeyConditionExpression': Key('cell').eq(cell) & Key('entity_key').begins_with(kind + '#'), 'Limit': min(100, maximum - len(features))}
        if cursor.get('last'):
            if cursor['last'].get('cell') != cell:
                raise ValueError('Cursor de celda inválido')
            options['ExclusiveStartKey'] = cursor['last']
        page = index.query(**options)
        for pointer in page.get('Items', []):
            examined += 1
            pk = 'antenna_id' if kind == 'ANTENNA' else 'zone_id'
            item = table.get_item(Key={pk: pointer['entity_id']}, ConsistentRead=True).get('Item')
            if not item:
                continue
            if query.get('zone_type') and item.get('zone_type') != query['zone_type']:
                continue
            if query.get('criticality') and item.get('criticality') != query['criticality']:
                continue
            geom = shape({'type': 'Point', 'coordinates': [float(item['longitude']), float(item['latitude'])]}) if kind == 'ANTENNA' else load_geometry(s3, item)
            if bounds and (not geom.intersects(box(*bounds))):
                continue
            owners = sorted(set(cells(geom.bounds)).intersection(selected)) if bounds else ['ZONES']
            if not owners or cell != owners[0]:
                continue
            features.append({'type': 'Feature', 'id': item[pk], 'geometry': mapping(geom), 'properties': item})
        if page.get('LastEvaluatedKey'):
            cursor = {'cell': position, 'last': page['LastEvaluatedKey']}
        else:
            position += 1
            cursor = {'cell': position}
    return {'type': 'FeatureCollection', 'features': features, 'cursor': encode_cursor(cursor, binding) if position < len(selected) else None}

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
        result = spatial_page(index, table, s3, 'ANTENNA', params(event))
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
