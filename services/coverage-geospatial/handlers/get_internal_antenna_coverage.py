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
        antenna = (event.get('pathParameters') or {}).get('antenna_id', '')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', antenna):
            return {'statusCode': 400, 'body': json.dumps({'message': 'antenna_id invalido'})}
        item = table.get_item(Key={'antenna_id': antenna}, ConsistentRead=True).get('Item')
        if not item:
            return {'statusCode': 404, 'body': json.dumps({'message': 'Datos de antena no encontrados'})}
        return {'statusCode': 200, 'headers': {'Content-Type': 'application/json'}, 'body': json.dumps(item, default=str)}
    except Exception:
        return {'statusCode': 503, 'body': json.dumps({'message': 'Dependencia no disponible'})}
