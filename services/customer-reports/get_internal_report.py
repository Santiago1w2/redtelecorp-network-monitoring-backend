import json
import os
import re
import boto3
db = boto3.resource('dynamodb')
table = db.Table(os.environ['REPORTS_TABLE'])
def lambda_handler(event, context):
    identity = (event.get('requestContext') or {})
    if not identity.get('identity', {}).get('userArn') and not identity.get('authorizer', {}).get('iam', {}).get('userArn'):
        return {'statusCode': 403, 'body': '{}'}
    try:
        key = event['pathParameters']['report_id']
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', key):
            return {'statusCode': 400, 'body': '{}'}
        item = table.get_item(Key={'reportId': key}, ConsistentRead=True).get('Item')
        if not item:
            return {'statusCode': 404, 'body': '{}'}
        return {'statusCode': 200, 'body': json.dumps({'report': item}, default=str)}
    except Exception:
        return {'statusCode': 503, 'body': '{}'}
