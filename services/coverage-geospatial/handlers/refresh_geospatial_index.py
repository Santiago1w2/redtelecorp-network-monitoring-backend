"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
from boto3.dynamodb.types import TypeDeserializer
import boto3
import os
deserializer = TypeDeserializer()
dynamodb = boto3.resource('dynamodb')
index = dynamodb.Table(os.environ.get('SPATIAL_INDEX_TABLE', 'rtc-spatial-index-dev'))

def lambda_handler(event, context):
    for record in event.get('Records', []):
        images = record['dynamodb']
        old = {k: deserializer.deserialize(v) for k, v in images.get('OldImage', {}).items()}
        new = {k: deserializer.deserialize(v) for k, v in images.get('NewImage', {}).items()}
        item = new or old
        kind, key = ('ANTENNA', 'antenna_id') if 'antenna_id' in item else ('ZONE', 'zone_id')
        entity = item[key]
        with index.batch_writer() as writer:
            for cell in set(old.get('index_cells', [])) - set(new.get('index_cells', [])):
                writer.delete_item(Key={'cell': cell, 'entity_key': kind + '#' + entity})
            for cell in new.get('index_cells', []):
                writer.put_item(Item={'cell': cell, 'entity_key': kind + '#' + entity, 'entity_id': entity})
    return {'indexed': True}
