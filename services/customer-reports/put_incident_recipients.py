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
    if not ((event.get('requestContext') or {}).get('authorizer') or {}).get('iam', {}).get('userArn') and not ((event.get('requestContext') or {}).get('identity') or {}).get('userArn'):
        return {'statusCode': 403, 'body': json.dumps({'message': 'Identidad IAM requerida'})}
    try:
        incident = identifier(event['pathParameters']['incident_id'])
        raw = event.get('body', '{}')
        if event.get('isBase64Encoded'):
            raw = base64.b64decode(raw).decode()
        data = json.loads(raw)
        users = data['recipient_ids']
        if not isinstance(users, list) or not 1 <= len(users) <= 100:
            raise ValueError('Entre 1 y 100 destinatarios')
        users = sorted(set(identifier(user) for user in users))
        with links.batch_writer() as writer:
            for user in users:
                writer.put_item(Item={'incident_id': incident, 'recipient_id': user})
        return {'statusCode': 200, 'body': json.dumps({'incident_id': incident, 'linked': len(users)})}
    except (ValueError, TypeError, KeyError):
        return {'statusCode': 400, 'body': json.dumps({'message': 'Datos invalidos'})}
    except Exception:
        return {'statusCode': 503, 'body': json.dumps({'message': 'Reports no disponible'})}
