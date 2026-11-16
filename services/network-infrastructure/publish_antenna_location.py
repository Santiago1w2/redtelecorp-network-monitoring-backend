"""DynamoDB Streams -> EventBridge -> Coverage, sin escribir tablas de otros servicios."""
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeDeserializer

dynamodb = boto3.resource('dynamodb')
antennas = dynamodb.Table(os.environ['ANTENNAS_TABLE'])
stations = dynamodb.Table(os.environ['STATIONS_TABLE'])
events = boto3.client('events')
BUS = os.environ['EVENT_BUS_NAME']
deserialize = TypeDeserializer()
logger = logging.getLogger(__name__)


def decode(image):
    return {key: deserialize.deserialize(value) for key, value in image.items()}


def publish_location(antenna_id, record):
    # Se consulta el estado actual para que la repetición de un Stream antiguo no restaure coordenadas antiguas.
    antenna = antennas.get_item(Key={'antenna_id': antenna_id}, ConsistentRead=True).get('Item')
    if not antenna:
        return
    station = stations.get_item(Key={'stationId': antenna['stationId']}, ConsistentRead=True).get('Item')
    if not station:
        raise ValueError('Estacion de la antena inexistente')
    occurred = datetime.fromtimestamp(float(record['dynamodb']['ApproximateCreationDateTime']), timezone.utc)
    sequence = record['dynamodb']['SequenceNumber'].zfill(64)
    digest = hashlib.sha256((record['eventID'] + ':' + antenna_id).encode()).hexdigest()[:24]
    envelope = {
        'event_id': 'LOC-' + sequence + '-' + digest,
        'event_type': 'infrastructure.antenna.location.changed',
        'source': 'network-infrastructure',
        'occurred_at': occurred.isoformat(timespec='microseconds').replace('+00:00', 'Z'),
        'version': '1.0',
        # zoneId de 02 no garantiza una zona poligonal existente en 04: no se inventa esa asociación.
        'data': {'antenna_id': antenna_id, 'latitude': station['latitude'], 'longitude': station['longitude']},
    }
    result = events.put_events(Entries=[{
        'EventBusName': BUS,
        'Source': envelope['source'],
        'DetailType': envelope['event_type'],
        'Detail': json.dumps(envelope, default=lambda value: float(value) if isinstance(value, Decimal) else str(value)),
    }])
    if result.get('FailedEntryCount'):
        raise RuntimeError('EventBridge rechazo el cambio de ubicacion')


def lambda_handler(event, context):
    published = 0
    for record in event['Records']:
        if record.get('eventName') not in ('INSERT', 'MODIFY'):
            continue
        data = record['dynamodb']
        item = decode(data.get('NewImage', {}))
        old = decode(data.get('OldImage', {}))
        if 'antenna_id' in item:
            if old and item.get('stationId') == old.get('stationId'):
                continue
            ids = [item['antenna_id']]
        else:
            if all(item.get(key) == old.get(key) for key in ('latitude', 'longitude')):
                continue
            options = {'IndexName': 'stationId-index', 'KeyConditionExpression': Key('stationId').eq(item['stationId'])}
            ids = []
            while True:
                page = antennas.query(**options)
                ids.extend(row['antenna_id'] for row in page.get('Items', []))
                if not page.get('LastEvaluatedKey'):
                    break
                options['ExclusiveStartKey'] = page['LastEvaluatedKey']
        for antenna_id in ids:
            publish_location(antenna_id, record)
            published += 1
    return {'published': published}
