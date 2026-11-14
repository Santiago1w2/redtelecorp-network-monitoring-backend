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
from shapely.geometry import Polygon, mapping
import math
from pyproj import Geod
from datetime import datetime, timezone
import hashlib
import base64
logger = logging.getLogger(__name__)
INFRASTRUCTURE_URL = os.environ.get('INFRASTRUCTURE_API_URL', '')
MAX_CELLS = 256
CELL_SIZE = 0.1
GEOD = Geod(ellps='WGS84')
s3 = boto3.client('s3')
BUCKET = os.environ.get('GEOSPATIAL_BUCKET', '')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('COVERAGE_TABLE', 'rtc-coverage-dev'))
zones = dynamodb.Table(os.environ.get('ZONES_TABLE', 'rtc-geozones-dev'))

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

def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise Problem(400, 'Número inválido')
    result = Decimal(str(value))
    if not result.is_finite() or (low is not None and result < Decimal(str(low))) or (high is not None and result > Decimal(str(high))):
        raise Problem(400, 'Número fuera de rango')
    return result

def cells(bounds):
    a, b, c, d = bounds
    x1, x2 = (math.floor((a + 180) / CELL_SIZE), min(3599, math.floor((c + 180) / CELL_SIZE)))
    y1, y2 = (math.floor((b + 90) / CELL_SIZE), min(1799, math.floor((d + 90) / CELL_SIZE)))
    x1, y1 = (min(x1, 3599), min(y1, 1799))
    if (x2 - x1 + 1) * (y2 - y1 + 1) > MAX_CELLS:
        raise Problem(422, 'Área demasiado amplia; subdivida el bbox/geometría')
    return [f'{x:04d}:{y:04d}' for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)]

def circle(latitude, longitude, radius):
    points = [GEOD.fwd(float(longitude), float(latitude), angle, float(radius))[:2] for angle in range(0, 360, 3)]
    result = Polygon(points)
    if result.bounds[2] - result.bounds[0] > 180 or result.bounds[1] < -89 or result.bounds[3] > 89:
        raise Problem(422, 'Cobertura polar/antimeridiano requiere geometría dividida')
    cells(result.bounds)
    return result

def geohash(latitude, longitude, precision=7):
    alphabet = '0123456789bcdefghjkmnpqrstuvwxyz'
    ranges, output, bits, value, even = ([[-180.0, 180.0], [-90.0, 90.0]], [], 0, 0, True)
    while len(output) < precision:
        index = 0 if even else 1
        coordinate = float(longitude if even else latitude)
        middle = sum(ranges[index]) / 2
        value = value << 1 | int(coordinate >= middle)
        ranges[index][0 if coordinate >= middle else 1] = middle
        even, bits = (not even, bits + 1)
        if bits == 5:
            output.append(alphabet[value])
            bits, value = (0, 0)
    return ''.join(output)

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def decimalize(value):
    return json.loads(dumps(value), parse_float=Decimal)

def save_coverage(antenna, data, location_version=None):
    latitude, longitude = (number(data['latitude'], -90, 90), number(data['longitude'], -180, 180))
    radius = number(data['coverage_radius_m'], high=100000)
    if radius <= 0 or data.get('coverage_model', 'ESTIMATED_CIRCLE') != 'ESTIMATED_CIRCLE':
        raise Problem(400, 'Radio/modelo inválido')
    zone = data.get('zone_id')
    if zone:
        identifier(zone)
        if not zones.get_item(Key={'zone_id': zone}).get('Item'):
            raise Problem(404, 'Zona inexistente')
    geom = circle(latitude, longitude, radius)
    content = dumps(mapping(geom)).encode()
    key = f'geospatial/coverage/{antenna}/{hashlib.sha256(content).hexdigest()}.geojson'
    s3.put_object(Bucket=BUCKET, Key=key, Body=content, ContentType='application/geo+json', ServerSideEncryption='AES256')
    item = {'antenna_id': antenna, 'latitude': latitude, 'longitude': longitude, 'coverage_radius_m': radius, 'coverage_model': 'ESTIMATED_CIRCLE', 'zone_id': zone, 'geohash': geohash(latitude, longitude), 'geometry_s3_key': key, 'updated_at': now(), 'index_cells': cells([float(longitude), float(latitude), float(longitude), float(latitude)])}
    if location_version:
        item['location_version'] = location_version
        table.put_item(Item=decimalize(item), ConditionExpression='attribute_not_exists(location_version) OR location_version < :v', ExpressionAttributeValues={':v': location_version})
    else:
        previous = table.get_item(Key={'antenna_id': antenna}, ConsistentRead=True).get('Item', {})
        if previous.get('location_version'):
            item['location_version'] = previous['location_version']
        table.put_item(Item=decimalize(item))
    return item

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
    principal(event, 'coverage:write')
    antenna = path(event, 'antennaId')
    existing = external_json(INFRASTRUCTURE_URL, '/v1/infrastructure/antennas/' + antenna)
    if existing.get('antenna_id') != antenna:
        raise Problem(404, 'Antena inexistente')
    return response(200, save_coverage(antenna, body(event)))
