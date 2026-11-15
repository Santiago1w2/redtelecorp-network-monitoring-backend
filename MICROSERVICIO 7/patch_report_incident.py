import json
import os
import re
import boto3
from botocore.exceptions import ClientError

table = boto3.resource('dynamodb').Table(os.environ['REPORTS_TABLE'])

def lambda_handler(event, context):
    caller = event.get('requestContext') or {}
    if not caller.get('identity', {}).get('userArn') and not caller.get('authorizer', {}).get('iam', {}).get('userArn'):
        return {'statusCode': 403, 'body': '{}'}
    try:
        report = event['pathParameters']['report_id']
        data = json.loads(event.get('body', '{}'))
        if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}', value) for value in (report, data['incident_id'])):
            raise ValueError('Identificador invalido')
        statuses = {'DETECTADO': 'IN_REVIEW', 'EN_REVISION': 'IN_REVIEW', 'EN_ATENCION': 'IN_PROGRESS', 'SIENDO_ATENDIDA': 'IN_PROGRESS', 'SOLUCIONADA': 'RESOLVED'}
        status = statuses[data['status']]
        from datetime import datetime, timezone
        timestamp = datetime.fromisoformat(data['updated_at'].replace('Z', '+00:00'))
        if timestamp.tzinfo is None:
            raise ValueError('Timestamp sin zona')
        timestamp = timestamp.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
        if not table.get_item(Key={'reportId': report}, ConsistentRead=True).get('Item'):
            return {'statusCode': 404, 'body': '{}'}
        table.update_item(Key={'reportId': report},
            UpdateExpression='SET incident_id = :i, incident_status = :s, incident_updated_at = :t, #st = :rs, updatedAt = :t',
            ConditionExpression='attribute_exists(reportId) AND (attribute_not_exists(incident_updated_at) OR incident_updated_at <= :t)',
            ExpressionAttributeNames={'#st': 'status'},
            ExpressionAttributeValues={':i': data['incident_id'], ':s': data['status'], ':t': timestamp, ':rs': status})
        return {'statusCode': 200, 'body': json.dumps({'report_id': report, 'status': status})}
    except ClientError as error:
        if error.response['Error']['Code'] == 'ConditionalCheckFailedException':
            return {'statusCode': 200, 'body': json.dumps({'ignored_stale': True})}
        return {'statusCode': 503, 'body': '{}'}
    except (ValueError, TypeError, KeyError):
        return {'statusCode': 400, 'body': '{}'}
