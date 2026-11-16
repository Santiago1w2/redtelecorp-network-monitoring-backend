"""Análisis de impacto y construcción local del paquete de esta API.

Antes de desplegar: python handlers/post_impact_analysis.py --build-package
No ejecuta AWS: descarga las librerías Linux y genera .package/coverage-geospatial.zip.
"""
import sys

def build_package():
    import hashlib
    import json
    import subprocess
    from pathlib import Path
    from zipfile import ZipFile, ZIP_DEFLATED
    service = Path(__file__).resolve().parents[1]
    requirements = ['shapely==2.2.0', 'pyproj==3.8.0', 'oracledb==4.0.2']
    digest = hashlib.sha256(('python3.12|' + '|'.join(requirements)).encode()).hexdigest()[:12]
    dependencies = service / '.package' / ('python312-' + digest)
    dependencies.mkdir(parents=True, exist_ok=True)
    command = ['uv', 'pip', 'install', '--python', sys.executable, '--python-platform', 'x86_64-manylinux_2_34', '--python-version', '3.12', '--only-binary', ':all:', '--target', str(dependencies), '--cache-dir', str(service.parent / '.uv-cache'), *requirements]
    if '--offline' in sys.argv:
        command.append('--offline')
    subprocess.run(command, check=True)
    artifact = service / '.package' / 'coverage-geospatial.zip'
    with ZipFile(artifact, 'w', ZIP_DEFLATED) as package:
        for file in dependencies.rglob('*'):
            if file.is_file() and '__pycache__' not in file.parts:
                package.write(file, file.relative_to(dependencies).as_posix())
        for file in (service / 'handlers').glob('*.py'):
            package.write(file, 'handlers/' + file.name)
    print(json.dumps({'artifact': str(artifact), 'runtime': 'python3.12', 'bytes': artifact.stat().st_size}))
if __name__ == '__main__' and '--build-package' in sys.argv:
    build_package()
    raise SystemExit(0)
from decimal import Decimal
import json
import os
HEADERS = {'Content-Type': 'application/json', 'Access-Control-Allow-Origin': os.environ.get('FRONTEND_ORIGIN', 'https://localhost'), 'Vary': 'Origin', 'Cache-Control': 'no-store'}
import logging
import base64
import re
from shapely.ops import unary_union
from shapely import transform
from shapely.geometry import Polygon, shape, mapping
import math
from pyproj import Geod, CRS, Transformer
import boto3
import hashlib
from boto3.dynamodb.conditions import Key
logger = logging.getLogger(__name__)
MAX_CELLS = 256
CELL_SIZE = 0.1
GEOD = Geod(ellps='WGS84')
s3 = boto3.client('s3')
BUCKET = os.environ.get('GEOSPATIAL_BUCKET', '')
secrets = boto3.client('secretsmanager')
dynamodb = boto3.resource('dynamodb')
coverage_table = dynamodb.Table(os.environ.get('COVERAGE_TABLE', 'rtc-coverage-dev'))
index = dynamodb.Table(os.environ.get('SPATIAL_INDEX_TABLE', 'rtc-spatial-index-dev'))
zones_table = dynamodb.Table(os.environ.get('ZONES_TABLE', 'rtc-geozones-dev'))

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

def circle(latitude, longitude, radius):
    points = [GEOD.fwd(float(longitude), float(latitude), angle, float(radius))[:2] for angle in range(0, 360, 3)]
    result = Polygon(points)
    if result.bounds[2] - result.bounds[0] > 180 or result.bounds[1] < -89 or result.bounds[3] > 89:
        raise OverflowError('Cobertura polar/antimeridiano requiere geometría dividida')
    cells(result.bounds)
    return result

def load_geometry(s3, item):
    return shape(json.loads(s3.get_object(Bucket=os.environ['GEOSPATIAL_BUCKET'], Key=item['geometry_s3_key'])['Body'].read()))

def analyze_oracle_spatial(coverage, zones):
    import oracledb
    secret = json.loads(secrets.get_secret_value(SecretId=os.environ['ORACLE_SECRET_ARN'])['SecretString'])
    if not secret['dsn'].lower().startswith('tcps://'):
        raise ValueError('Oracle requires TCPS DSN')
    result = []
    with oracledb.connect(user=secret['username'], password=secret['password'], dsn=secret['dsn'], ssl_server_dn_match=True, tcp_connect_timeout=5) as connection:
        connection.call_timeout = 10000
        with connection.cursor() as cursor:
            cursor.setinputsizes(coverage=oracledb.DB_TYPE_CLOB, zone=oracledb.DB_TYPE_CLOB)
            for item, geom in zones:
                cursor.execute("\n                    SELECT SDO_GEOM.SDO_AREA(\n                        SDO_GEOM.SDO_INTERSECTION(SDO_UTIL.FROM_GEOJSON(:coverage),\n                                                  SDO_UTIL.FROM_GEOJSON(:zone), 0.05),\n                        0.05, 'unit=SQ_KM') FROM DUAL\n                ", coverage=dumps(coverage), zone=dumps(geom))
                area = cursor.fetchone()[0]
                if area and area > 0:
                    result.append({'zone_id': item['zone_id'], 'affected_area_km2': float(area)})
    return result

def lambda_handler(event, context):
    try:
        # Esta Lambda solo tiene una ruta API Gateway AWS_IAM.
        if not ((event.get('requestContext') or {}).get('identity') or {}).get('userArn'):
            return {'statusCode': 403, 'body': json.dumps({'message': 'Identidad IAM requerida'})}
        request_data = body(event)
        if len(dumps(request_data).encode('utf-8')) > 200000:
            return {
                'statusCode': 413,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Solicitud demasiado grande'}, default=str),
            }
        data = request_data
        incident = identifier(data['incident_id'])
        antenna_ids = data['antenna_ids']
        if not isinstance(antenna_ids, list) or not 1 <= len(antenna_ids) <= 30:
            return {
                'statusCode': 400,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Se requieren entre 1 y 30 antenas'}, default=str),
            }
        coverages, candidate_ids = ([], set())
        for antenna in set((identifier(x) for x in antenna_ids)):
            item = coverage_table.get_item(Key={'antenna_id': antenna}, ConsistentRead=True).get('Item')
            if not item:
                return {
                    'statusCode': 404,
                    'headers': HEADERS,
                    'body': json.dumps({'message': 'Antena sin cobertura'}, default=str),
                }
            geom = circle(item['latitude'], item['longitude'], item['coverage_radius_m'])
            coverages.append(geom)
            for cell in cells(geom.bounds):
                options = {'KeyConditionExpression': Key('cell').eq(cell) & Key('entity_key').begins_with('ZONE#')}
                while True:
                    page = index.query(**options)
                    candidate_ids.update((x['entity_id'] for x in page.get('Items', [])))
                    if len(candidate_ids) > 2000:
                        return {
                            'statusCode': 422,
                            'headers': HEADERS,
                            'body': json.dumps({'message': 'Demasiadas zonas; reduzca el análisis'}, default=str),
                        }
                    if not page.get('LastEvaluatedKey'):
                        break
                    options['ExclusiveStartKey'] = page['LastEvaluatedKey']
        combined = unary_union(coverages)
        if combined.bounds[2] - combined.bounds[0] > 10 or combined.bounds[3] - combined.bounds[1] > 10:
            return {
                'statusCode': 422,
                'headers': HEADERS,
                'body': json.dumps({'message': 'Análisis regional demasiado amplio'}, default=str),
            }
        center = combined.centroid
        local = CRS.from_proj4(f'+proj=laea +lat_0={center.y} +lon_0={center.x} +datum=WGS84 +units=m')
        projection = Transformer.from_crs('EPSG:4326', local, always_xy=True).transform
        projected = transform(combined, projection, interleaved=False)
        zones, affected, intersections = ([], [], [])
        for zone in sorted(candidate_ids):
            item = zones_table.get_item(Key={'zone_id': zone}, ConsistentRead=True).get('Item')
            if not item:
                continue
            geom = load_geometry(s3, item)
            zones.append((item, mapping(geom)))
            intersection = projected.intersection(transform(geom, projection, interleaved=False))
            if not intersection.is_empty and intersection.area > 0:
                affected.append({'zone_id': zone, 'name': item['name'], 'criticality': item['criticality'], 'affected_area_km2': intersection.area / 1000000})
                intersections.append(intersection)
        oracle_status = 'DISABLED'
        if os.environ.get('ORACLE_ENABLED', 'false') == 'true':
            try:
                oracle_results = analyze_oracle_spatial(mapping(combined), zones)
                oracle_status = 'VERIFIED_REQUEST'
            except Exception as exc:
                logger.warning('oracle_fallback type=%s', type(exc).__name__)
                oracle_status, oracle_results = ('FALLBACK_LOCAL', None)
        else:
            oracle_results = None
        ranking = {'LOW': 1, 'MEDIUM': 2, 'HIGH': 3, 'CRITICAL': 4}
        result = {'incident_id': incident, 'affected_zones': affected, 'criticality': max((x['criticality'] for x in affected), key=ranking.get, default='NONE'), 'estimated_affected_area_km2': unary_union(intersections).area / 1000000 if intersections else 0, 'analysis_method': 'WGS84_GEODESIC_CIRCLES_LAEA_SHAPELY', 'oracle_status': oracle_status}
        if oracle_results is not None:
            result['oracle_spatial_results'] = oracle_results
        artifact = {'type': 'FeatureCollection', 'features': [{'type': 'Feature', 'properties': {'incident_id': incident}, 'geometry': mapping(combined)}], 'analysis': result}
        key = f'geospatial/analysis/{incident}/{hashlib.sha256(dumps(artifact).encode()).hexdigest()}.geojson'
        s3.put_object(Bucket=BUCKET, Key=key, Body=dumps(artifact).encode(), ContentType='application/geo+json', ServerSideEncryption='AES256')
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
