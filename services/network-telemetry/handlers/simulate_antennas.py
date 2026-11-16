"""Development antenna simulator: EventBridge -> Lambda -> existing ingest SQS."""
import hashlib
import json
import os
import random
import re
import uuid
from datetime import datetime, timezone
from urllib import error, request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def settings():
    antennas = [item.strip() for item in os.environ.get('SIMULATOR_ANTENNA_IDS', '').split(',') if item.strip()]
    if len(antennas) != 20 or len(set(antennas)) != 20:
        raise ValueError('Configure exactly 20 distinct, registered simulator antenna IDs')
    if any(not re.fullmatch(r'ANT-SIM-[a-f0-9]{32}', item) for item in antennas):
        raise ValueError('Only ANT-SIM-<uuid> demo antennas are allowed')
    count = int(os.environ.get('SIMULATOR_SAMPLE_SIZE', '5'))
    if not 1 <= count <= 20:
        raise ValueError('SIMULATOR_SAMPLE_SIZE must be between 1 and 20')
    if os.environ.get('STAGE', 'dev') != 'dev':
        raise ValueError('Simulator is restricted to dev')
    return antennas, count


def registered_antenna(antenna):
    base = os.environ.get('INFRASTRUCTURE_API_URL', '').rstrip('/')
    if not base.startswith('https://'):
        raise ValueError('Configure the HTTPS infrastructure API URL')
    url = base + '/v1/infrastructure/antennas/' + antenna
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    signed = AWSRequest(method='GET', url=url)
    SigV4Auth(credentials, 'execute-api', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(signed)
    try:
        with request.urlopen(request.Request(url, headers=dict(signed.headers.items())), timeout=5) as response:
            item = json.loads(response.read(100000))
    except (error.URLError, TimeoutError) as exc:
        raise RuntimeError('Infrastructure lookup failed; no measurements submitted') from exc
    if item.get('antenna_id') != antenna:
        raise ValueError('Demo antenna is not registered')


def measurements(event, context=None):
    antennas, count = settings()
    # EventBridge retries retain id/time: regenerate the same values for deduplication.
    event_id = event.get('id') or getattr(context, 'aws_request_id', None) or uuid.uuid4().hex
    date = datetime.fromisoformat(event['time'].replace('Z', '+00:00')) if event.get('time') else datetime.now(timezone.utc)
    if date.tzinfo is None:
        raise ValueError('Timestamp must include a timezone')
    stamp = date.astimezone(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')
    rng = random.Random(int(hashlib.sha256(str(event_id).encode()).hexdigest(), 16))
    result = []
    for antenna in rng.sample(antennas, count):
        state = rng.choices(['NORMAL', 'DOWN', 'SATURATED', 'DISABLED'], weights=[70, 15, 10, 5])[0]
        value = {'NORMAL': (60, 129), 'DOWN': (1, 59), 'SATURATED': (130, 180), 'DISABLED': (0, 0)}[state]
        result.append({'antenna_id': antenna, 'timestamp': stamp, 'traffic_gb_min': rng.randint(*value)})
    return result


def lambda_handler(event, context):
    batch = measurements(event or {}, context)
    queue = os.environ.get('INGEST_QUEUE_URL', '')
    if not queue:
        raise ValueError('INGEST_QUEUE_URL is required')
    # Validate the complete sample before publishing. Do not create phantom inventory.
    for item in batch:
        registered_antenna(item['antenna_id'])
    sqs = boto3.client('sqs')
    for start in range(0, len(batch), 10):
        chunk = batch[start:start + 10]
        response = sqs.send_message_batch(QueueUrl=queue, Entries=[
            {'Id': str(index), 'MessageBody': json.dumps(item)} for index, item in enumerate(chunk)
        ])
        expected = {str(index) for index in range(len(chunk))}
        if response.get('Failed') or {item['Id'] for item in response.get('Successful', [])} != expected:
            raise RuntimeError('SQS partially failed; retry with the same event id/time')
    return {'submitted': len(batch), 'antenna_ids': [item['antenna_id'] for item in batch], 'timestamp': batch[0]['timestamp']}
