import base64
import json
import os
import re
import boto3
from boto3.dynamodb.conditions import Key

db = boto3.resource('dynamodb')
links = db.Table(os.environ['RECIPIENTS_TABLE'])

def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value):
        raise ValueError('Identificador invalido')
    return value

def lambda_handler(event, context):
    if not ((event.get('requestContext') or {}).get('http', {}).get('method') or event.get('httpMethod')):
        return {'statusCode': 400, 'body': '{}'}
    if not ((event.get('requestContext') or {}).get('authorizer') or {}).get('iam', {}).get('userArn') and not ((event.get('requestContext') or {}).get('identity') or {}).get('userArn'):
        return {'statusCode': 403, 'body': json.dumps({'message': 'Identidad IAM requerida'})}
    try:
        incident = identifier(event['pathParameters']['incident_id'])
        query = event.get('queryStringParameters') or {}
        limit = int(query.get('limit', '100'))
        if not 1 <= limit <= 100:
            raise ValueError('limit invalido')
        options = {'KeyConditionExpression': Key('incident_id').eq(incident), 'Limit': limit, 'ConsistentRead': True}
        if query.get('cursor'):
            key = json.loads(base64.urlsafe_b64decode(query['cursor']).decode())
            if key.get('incident_id') != incident:
                raise ValueError('Cursor de otro incidente')
            options['ExclusiveStartKey'] = key
        page = links.query(**options)
        cursor = base64.urlsafe_b64encode(json.dumps(page['LastEvaluatedKey']).encode()).decode() if page.get('LastEvaluatedKey') else None
        return {'statusCode': 200, 'body': json.dumps({'recipient_ids': [item['recipient_id'] for item in page.get('Items', [])], 'cursor': cursor})}
    except (ValueError, TypeError, KeyError):
        return {'statusCode': 400, 'body': json.dumps({'message': 'Consulta invalida'})}
    except Exception:
        return {'statusCode': 503, 'body': json.dumps({'message': 'Reports no disponible'})}
