"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
import re
from datetime import datetime, timezone
from botocore.exceptions import ClientError
from decimal import Decimal
from shapely.geometry import Polygon, mapping
import math
from pyproj import Geod
import boto3
import os
import json
import hashlib
MAX_CELLS = 256
CELL_SIZE = 0.1
GEOD = Geod(ellps='WGS84')
s3 = boto3.client('s3')
BUCKET = os.environ.get('GEOSPATIAL_BUCKET', '')
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('COVERAGE_TABLE', 'rtc-coverage-dev'))
zones = dynamodb.Table(os.environ.get('ZONES_TABLE', 'rtc-geozones-dev'))

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador inválido')
    return value

def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise ValueError('Timestamp ISO 8601 requerido')
    dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if dt.tzinfo is None:
        raise ValueError('El timestamp requiere zona horaria')
    return dt.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def event_envelope(event, source, types):
    detail = event.get('detail', {})
    if event.get('source') != source or event.get('detail-type') not in types:
        raise ValueError('Origen de evento inválido')
    if detail.get('version') != '1.0' or detail.get('source') != source or detail.get('event_type') != event['detail-type']:
        raise ValueError('Contrato de evento inválido')
    identifier(detail['event_id'])
    timestamp(detail['occurred_at'])
    return detail

def number(value, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError('Número inválido')
    result = Decimal(str(value))
    if not result.is_finite() or (low is not None and result < Decimal(str(low))) or (high is not None and result > Decimal(str(high))):
        raise ValueError('Número fuera de rango')
    return result

def cells(bounds):
    a, b, c, d = bounds
    x1, x2 = (math.floor((a + 180) / CELL_SIZE), min(3599, math.floor((c + 180) / CELL_SIZE)))
    y1, y2 = (math.floor((b + 90) / CELL_SIZE), min(1799, math.floor((d + 90) / CELL_SIZE)))
    x1, y1 = (min(x1, 3599), min(y1, 1799))
    if (x2 - x1 + 1) * (y2 - y1 + 1) > MAX_CELLS:
        raise OverflowError('Área demasiado amplia; subdivida el bbox/geometría')
    return [f'{x:04d}:{y:04d}' for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)]

def circle(latitude, longitude, radius):
    points = [GEOD.fwd(float(longitude), float(latitude), angle, float(radius))[:2] for angle in range(0, 360, 3)]
    result = Polygon(points)
    if result.bounds[2] - result.bounds[0] > 180 or result.bounds[1] < -89 or result.bounds[3] > 89:
        raise OverflowError('Cobertura polar/antimeridiano requiere geometría dividida')
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

def decimalize(value):
    return json.loads(dumps(value), parse_float=Decimal)

def save_coverage(antenna, data, location_version=None):
    latitude, longitude = (number(data['latitude'], -90, 90), number(data['longitude'], -180, 180))
    radius = number(data['coverage_radius_m'], high=100000)
    if radius <= 0 or data.get('coverage_model', 'ESTIMATED_CIRCLE') != 'ESTIMATED_CIRCLE':
        raise ValueError('Radio/modelo inválido')
    zone = data.get('zone_id')
    if zone:
        identifier(zone)
        if not zones.get_item(Key={'zone_id': zone}).get('Item'):
            raise LookupError('Zona inexistente')
    geom = circle(latitude, longitude, radius)
    content = dumps(mapping(geom)).encode()
    key = f'geospatial/coverage/{antenna}/{hashlib.sha256(content).hexdigest()}.geojson'
    s3.put_object(Bucket=BUCKET, Key=key, Body=content, ContentType='application/geo+json', ServerSideEncryption='AES256')
    item = {'antenna_id': antenna, 'latitude': latitude, 'longitude': longitude, 'coverage_radius_m': radius, 'coverage_model': 'ESTIMATED_CIRCLE', 'zone_id': zone, 'geohash': geohash(latitude, longitude), 'geometry_s3_key': key, 'updated_at': now(), 'index_cells': cells([float(longitude), float(latitude), float(longitude), float(latitude)])}
    if not zone:
        item.pop('zone_id', None)
    if location_version:
        item['location_version'] = location_version
        table.put_item(Item=decimalize(item), ConditionExpression='attribute_not_exists(location_version) OR location_version < :v', ExpressionAttributeValues={':v': location_version})
    else:
        previous = table.get_item(Key={'antenna_id': antenna}, ConsistentRead=True).get('Item', {})
        if previous.get('location_version'):
            item['location_version'] = previous['location_version']
        table.put_item(Item=decimalize(item))
    return item

def lambda_handler(event, context):
    detail = event_envelope(event, 'network-infrastructure', ['infrastructure.antenna.location.changed'])
    data = detail['data']
    antenna = identifier(data['antenna_id'])
    version = timestamp(detail['occurred_at']) + '#' + detail['event_id']
    previous = table.get_item(Key={'antenna_id': antenna}, ConsistentRead=True).get('Item', {})
    if previous.get('location_version', '') >= version:
        return {'duplicate_or_stale': True}
    merged = dict(previous, **data)
    merged.setdefault('coverage_radius_m', 1500)
    merged.setdefault('coverage_model', 'ESTIMATED_CIRCLE')
    try:
        save_coverage(antenna, merged, version)
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
    return {'synced': True}
