"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
from collections import defaultdict
import boto3
import os
from boto3.dynamodb.types import TypeDeserializer
import hashlib
import gzip
from decimal import Decimal
import json
s3 = boto3.client('s3')
BUCKET = os.environ.get('HISTORY_BUCKET', '')
deserializer = TypeDeserializer()

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

def lambda_handler(event, context):
    groups = defaultdict(list)
    for record in event.get('Records', []):
        if record['eventName'] != 'INSERT':
            continue
        item = {k: deserializer.deserialize(v) for k, v in record['dynamodb']['NewImage'].items()}
        date = item['timestamp']
        prefix = f"telemetry/antenna_id={item['antenna_id']}/year={date[:4]}/month={date[5:7]}/day={date[8:10]}/"
        groups[prefix].append(item)
    for prefix, items in groups.items():
        items.sort(key=lambda x: x['measurement_key'])
        content = ('\n'.join((dumps(item) for item in items)) + '\n').encode()
        digest = hashlib.sha256(content).hexdigest()
        s3.put_object(Bucket=BUCKET, Key=prefix + 'measurements-' + digest + '.jsonl.gz', Body=gzip.compress(content, mtime=0), ContentType='application/x-ndjson', ContentEncoding='gzip', ServerSideEncryption='AES256')
    return {'archived': sum(map(len, groups.values()))}
