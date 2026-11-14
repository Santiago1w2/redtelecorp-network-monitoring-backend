"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import functools
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

def cells(bounds):
    a, b, c, d = bounds
    x1, x2 = (math.floor((a + 180) / CELL_SIZE), min(3599, math.floor((c + 180) / CELL_SIZE)))
    y1, y2 = (math.floor((b + 90) / CELL_SIZE), min(1799, math.floor((d + 90) / CELL_SIZE)))
    x1, y1 = (min(x1, 3599), min(y1, 1799))
    if (x2 - x1 + 1) * (y2 - y1 + 1) > MAX_CELLS:
        raise Problem(422, 'Área demasiado amplia; subdivida el bbox/geometría')
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
        raise Problem(400, 'bbox inválido: minLon,minLat,maxLon,maxLat')

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

def load_geometry(s3, item):
    return shape(json.loads(s3.get_object(Bucket=os.environ['GEOSPATIAL_BUCKET'], Key=item['geometry_s3_key'])['Body'].read()))

def spatial_page(index, table, s3, kind, query):
    bounds = bbox(query['bbox']) if query.get('bbox') else None
    if kind == 'ANTENNA' and bounds is None:
        raise Problem(400, 'bbox requerido')
    selected = cells(bounds) if bounds else ['ZONES']
    binding = [kind, bounds, query.get('zone_type'), query.get('criticality')]
    cursor = decode_cursor(query.get('cursor'), binding) or {'cell': 0}
    position = int(cursor.get('cell', 0))
    if not 0 <= position < len(selected):
        raise Problem(400, 'Cursor fuera de rango')
    maximum, features, examined = (limit(query.get('limit')), [], 0)
    while position < len(selected) and len(features) < maximum and (examined < 500):
        cell = selected[position]
        options = {'KeyConditionExpression': Key('cell').eq(cell) & Key('entity_key').begins_with(kind + '#'), 'Limit': min(100, maximum - len(features))}
        if cursor.get('last'):
            if cursor['last'].get('cell') != cell:
                raise Problem(400, 'Cursor de celda inválido')
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

@http
def lambda_handler(event, context):
    principal(event, 'coverage:read')
    return response(200, spatial_page(index, table, s3, 'ANTENNA', params(event)))
