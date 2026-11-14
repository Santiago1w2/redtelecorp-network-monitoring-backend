"""Lambda autocontenida: configuración, auxiliares y handler en este archivo."""
from botocore.exceptions import ClientError, BotoCoreError
from datetime import datetime, timezone
import boto3
import os
from botocore.awsrequest import AWSRequest
from urllib import error, request
from decimal import Decimal
import json
from botocore.auth import SigV4Auth
import time
import re
import html
import logging
from boto3.dynamodb.types import TypeDeserializer
EMAIL_TEMPLATES = {'INCIDENT_DETECTED': '<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Incidente en evaluación</title></head><body><h1>Estamos evaluando una incidencia</h1><p>Detectamos un posible problema de red en {{zone_name}}.</p><p>Estado: {{incident_status}}.</p><p>El equipo de Red TeleCorp está verificando el alcance. Informaremos los avances por tus canales habilitados.</p></body></html>\n', 'INCIDENT_IN_PROGRESS': '<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Incidente en atención</title></head><body><h1>Estamos atendiendo la incidencia</h1><p>Nuestro equipo trabaja para recuperar el servicio en {{zone_name}}.</p><p>Estado: {{incident_status}}.</p><p>Te avisaremos cuando la atención haya concluido. Red TeleCorp.</p></body></html>\n', 'INCIDENT_RESOLVED': '<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Servicio restablecido</title></head><body><h1>La incidencia fue resuelta</h1><p>El servicio de red en {{zone_name}} se encuentra restablecido.</p><p>Estado: {{incident_status}}.</p><p>Si continúas con dificultades, registra un reporte para que podamos revisarlo. Red TeleCorp.</p></body></html>\n'}
dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('NOTIFICATIONS_TABLE', 'rtc-notifications-dev'))
preferences = dynamodb.Table(os.environ.get('PREFERENCES_TABLE', 'rtc-notification-preferences-dev'))
IDENTITY_URL = os.environ.get('IDENTITY_API_URL', '')
PERMANENT = {'MessageRejected', 'MailFromDomainNotVerifiedException', 'InvalidParameter', 'InvalidParameterValue', 'AuthorizationError', 'AuthorizationErrorException', 'AccessDenied', 'AccessDeniedException', 'AccountSendingPausedException'}
sqs, ses, sns = (boto3.client('sqs'), boto3.client('ses'), boto3.client('sns'))
QUEUE = os.environ.get('SEND_QUEUE_URL', '')
logger = logging.getLogger(__name__)
deserializer = TypeDeserializer()

class Problem(Exception):

    def __init__(self, status, message):
        self.status, self.message = (status, message)

def now():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')

def update_channel(key, channel, status, **extra):
    names = {'#d': 'delivery_status', '#c': channel, '#s': 'status'}
    values = {':s': status, ':t': now()}
    expression = 'SET #d.#c.#s = :s, #d.#c.updated_at = :t'
    for index, (name, value) in enumerate(extra.items()):
        names['#x' + str(index)] = name
        values[':x' + str(index)] = value
        expression += f', #d.#c.#x{index} = :x{index}'
    table.update_item(Key=key, UpdateExpression=expression, ExpressionAttributeNames=names, ExpressionAttributeValues=values, ConditionExpression='attribute_exists(notification_key)')

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

def external_json(base_url, route, method='GET', payload=None):
    """API privada de otro equipo con AWS_IAM; TLS y tiempo límite explícito."""
    if not base_url:
        raise Problem(503, 'API de integración sin configurar')
    url = base_url.rstrip('/') + route
    if not url.startswith('https://') and (not (os.environ.get('LOCAL_TEST') == 'true' and url.startswith('http://127.0.0.1:'))):
        raise Problem(503, 'La integración requiere HTTPS')
    data = dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    if not os.environ.get('LOCAL_TEST') == 'true':
        credentials = boto3.Session().get_credentials().get_frozen_credentials()
        signed = AWSRequest(method=method, url=url, data=data, headers=headers)
        SigV4Auth(credentials, 'execute-api', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(signed)
        headers = dict(signed.headers.items())
    try:
        with request.urlopen(request.Request(url, data=data, headers=headers, method=method), timeout=5) as result:
            return json.loads(result.read(1000000), parse_float=Decimal)
    except error.HTTPError as exc:
        if exc.code == 404:
            raise Problem(404, 'Recurso externo no encontrado')
        raise Problem(503, 'API externa no disponible')
    except (error.URLError, TimeoutError):
        raise Problem(503, 'API externa no disponible')

def recipient(user):
    result = external_json(IDENTITY_URL, '/v1/identity/users/' + user + '/notification-contact')
    if result.get('user_id') != user or not result.get('active'):
        raise Problem(404, 'Destinatario inexistente/inactivo')
    return result

def send(key):
    item = table.get_item(Key=key, ConsistentRead=True).get('Item')
    if not item:
        raise Problem(404, 'Notificación no encontrada')
    prefs = preferences.get_item(Key={'user_id': item['recipient_id']}, ConsistentRead=True).get('Item', {})
    defaults = {'EMAIL': True, 'SMS': False, 'IN_APP': True}
    contact, failed = (None, False)
    for channel, state in item['delivery_status'].items():
        if state['status'] in ('SENT', 'DELIVERED', 'SKIPPED', 'FAILED', 'BOUNCED', 'COMPLAINT'):
            continue
        if state['status'] == 'UNKNOWN':
            failed = True
            continue
        if state['status'] == 'SENDING':
            if state.get('lock_until', 0) <= int(time.time()):
                update_channel(key, channel, 'UNKNOWN', error_code='AMBIGUOUS_PROVIDER_RESULT')
            failed = True
            continue
        if not prefs.get('incident_updates', True) or not prefs.get(channel.lower() + '_enabled', defaults[channel]):
            update_channel(key, channel, 'SKIPPED')
            continue
        if channel == 'IN_APP':
            update_channel(key, channel, 'DELIVERED')
            continue
        if os.environ.get('DELIVERY_ENABLED', 'false') != 'true':
            update_channel(key, channel, 'BLOCKED', error_code='DELIVERY_NOT_ENABLED')
            failed = True
            continue
        try:
            contact = contact or recipient(item['recipient_id'])
            if channel == 'EMAIL' and (not re.fullmatch('[^\\s@]+@[^\\s@]+\\.[^\\s@]+', contact.get('email', ''))):
                update_channel(key, channel, 'FAILED', error_code='CONTACT_UNAVAILABLE')
                continue
            if channel == 'SMS' and (not re.fullmatch('\\+[1-9][0-9]{7,14}', contact.get('phone_e164', ''))):
                update_channel(key, channel, 'FAILED', error_code='CONTACT_UNAVAILABLE')
                continue
            table.update_item(Key=key, UpdateExpression='SET #d.#c.#s = :sending, #d.#c.lock_until = :expiry, #d.#c.attempts = #d.#c.attempts + :one', ConditionExpression='#d.#c.#s IN (:pending, :retry, :blocked)', ExpressionAttributeNames={'#d': 'delivery_status', '#c': channel, '#s': 'status'}, ExpressionAttributeValues={':sending': 'SENDING', ':expiry': int(time.time()) + 120, ':one': 1, ':pending': 'PENDING', ':retry': 'RETRY', ':blocked': 'BLOCKED'})
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                failed = True
                continue
            raise
        except Problem:
            failed = True
            continue
        try:
            if channel == 'EMAIL':
                markup = EMAIL_TEMPLATES[item['template']]
                for name, value in item['variables'].items():
                    markup = markup.replace('{{' + name + '}}', html.escape(value))
                options = {'Source': os.environ['SES_FROM_ADDRESS'], 'Destination': {'ToAddresses': [contact['email']]}, 'Message': {'Subject': {'Data': item['title'], 'Charset': 'UTF-8'}, 'Body': {'Text': {'Data': item['message'], 'Charset': 'UTF-8'}, 'Html': {'Data': markup, 'Charset': 'UTF-8'}}}, 'Tags': [{'Name': 'notification_id', 'Value': item['notification_id']}]}
                if os.environ.get('SES_CONFIGURATION_SET'):
                    options['ConfigurationSetName'] = os.environ['SES_CONFIGURATION_SET']
                result = ses.send_email(**options)
            else:
                result = sns.publish(PhoneNumber=contact['phone_e164'], Message=item['message'], MessageAttributes={'AWS.SNS.SMS.SMSType': {'DataType': 'String', 'StringValue': 'Transactional'}})
        except ClientError as exc:
            code = exc.response['Error']['Code']
            update_channel(key, channel, 'FAILED' if code in PERMANENT else 'RETRY', error_code=code)
            failed |= code not in PERMANENT
            continue
        except BotoCoreError:
            update_channel(key, channel, 'UNKNOWN', error_code='AMBIGUOUS_PROVIDER_RESULT')
            failed = True
            continue
        update_channel(key, channel, 'SENT', provider_message_id=result['MessageId'])
    if failed:
        raise RuntimeError('Delivery requires retry or operator reconciliation')

def lambda_handler(event, context):
    failures = []
    for record in event.get('Records', []):
        is_stream = record.get('eventSource') == 'aws:dynamodb'
        try:
            if is_stream:
                if record['eventName'] != 'INSERT':
                    continue
                item = {k: deserializer.deserialize(v) for k, v in record['dynamodb']['NewImage'].items()}
                sqs.send_message(QueueUrl=QUEUE, MessageBody=dumps({k: item[k] for k in ('recipient_id', 'notification_key')}))
            else:
                send(json.loads(record['body']))
        except Exception as exc:
            logger.error('notification_retry type=%s', type(exc).__name__)
            if is_stream:
                raise
            failures.append({'itemIdentifier': record['messageId']})
    return {'batchItemFailures': failures}
