import base64
import json
import os
import re
import boto3
from boto3.dynamodb.conditions import Key

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ['COVERAGE_TABLE'])

def lambda_handler(event, context):
    if not ((event.get('requestContext') or {}).get('identity') or {}).get('userArn'):
        return {'statusCode': 403, 'body': json.dumps({'message': 'Identidad IAM requerida'})}
    try:
        zone = (event.get('pathParameters') or {}).get('zone_id', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', zone):
            raise ValueError('zone_id invalido')
        query = event.get('queryStringParameters') or {}
        limit = int(query.get('limit', '100'))
        if not 1 <= limit <= 100:
            raise ValueError('limit fuera de rango')
        options = {'IndexName': 'zone_id-index', 'KeyConditionExpression': Key('zone_id').eq(zone), 'Limit': limit}
        if query.get('cursor'):
            key = json.loads(base64.urlsafe_b64decode(query['cursor']).decode())
            if key.get('zone_id') != zone:
                raise ValueError('cursor de otra zona')
            options['ExclusiveStartKey'] = key
        page = table.query(**options)
        cursor = base64.urlsafe_b64encode(json.dumps(page['LastEvaluatedKey']).encode()).decode() if page.get('LastEvaluatedKey') else None
        return {'statusCode': 200, 'body': json.dumps({'antennas': page.get('Items', []), 'cursor': cursor}, default=str)}
    except (ValueError, TypeError, KeyError):
        return {'statusCode': 400, 'body': json.dumps({'message': 'Consulta invalida'})}
    except Exception:
        return {'statusCode': 503, 'body': json.dumps({'message': 'Dependencia no disponible'})}
