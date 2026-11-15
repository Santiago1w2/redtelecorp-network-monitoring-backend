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
import hashlib
import logging
from boto3.dynamodb.types import TypeDeserializer

dynamodb = boto3.resource('dynamodb')
table = dynamodb.Table(os.environ.get('NOTIFICATIONS_TABLE', 'rtc-notifications-dev'))
preferences = dynamodb.Table(os.environ.get('PREFERENCES_TABLE', 'rtc-notification-preferences-dev'))
IDENTITY_URL = os.environ.get('IDENTITY_API_URL', '')
PERMANENT = {'InvalidParameter', 'InvalidParameterValue', 'AuthorizationError', 'AuthorizationErrorException', 'AccessDenied', 'AccessDeniedException'}
sqs, sns = boto3.client('sqs'), boto3.client('sns')
QUEUE = os.environ.get('SEND_QUEUE_URL', '')
logger = logging.getLogger(__name__)
deserializer = TypeDeserializer()

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
        raise ConnectionError('API de integración sin configurar')
    url = base_url.rstrip('/') + route
    if not url.startswith('https://') and (not (os.environ.get('LOCAL_TEST') == 'true' and url.startswith('http://127.0.0.1:'))):
        raise ConnectionError('La integración requiere HTTPS')
    data = dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'}
    credentials = boto3.Session().get_credentials().get_frozen_credentials()
    signed = AWSRequest(method=method, url=url, data=data, headers=headers)
    SigV4Auth(credentials, 'execute-api', os.environ.get('AWS_REGION', 'us-east-1')).add_auth(signed)
    headers = dict(signed.headers.items())
    try:
        with request.urlopen(request.Request(url, data=data, headers=headers, method=method), timeout=5) as result:
            return json.loads(result.read(1000000), parse_float=Decimal)
    except error.HTTPError as exc:
        if exc.code == 404:
            raise LookupError('Recurso externo no encontrado')
        raise ConnectionError('API externa no disponible')
    except (error.URLError, TimeoutError):
        raise ConnectionError('API externa no disponible')

def recipient(user):
    result = external_json(IDENTITY_URL, '/v1/identity/users/' + user + '/notification-contact')
    if result.get('user_id') != user or not result.get('active'):
        raise LookupError('Destinatario inexistente/inactivo')
    return result

def email_subscription(user, email):
    # A separate topic for each user/email prevents cross-user disclosure,
    # including after a user's verified email changes.
    prefix = os.environ.get('SNS_EMAIL_TOPIC_PREFIX', 'rtc-notifications-email-dev')
    suffix = hashlib.sha256((user + '\0' + email).encode()).hexdigest()[:32]
    topic = sns.create_topic(Name=prefix + '-' + suffix)['TopicArn']
    token, seen, subscriptions = None, set(), []
    while True:
        args = {'TopicArn': topic}
        if token:
            args['NextToken'] = token
        page = sns.list_subscriptions_by_topic(**args)
        subscriptions.extend(page.get('Subscriptions', []))
        token = page.get('NextToken')
        if not token:
            break
        if token in seen:
            raise ConnectionError('Invalid SNS pagination')
        seen.add(token)
    # Never publish personal incident information into a topic with other subscribers.
    if any(sub.get('Protocol') != 'email' or sub.get('Endpoint') != email for sub in subscriptions):
        raise PermissionError('Unexpected recipient in personal SNS topic')
    if any(sub.get('SubscriptionArn', '').startswith('arn:') for sub in subscriptions):
        return topic, True
    if not subscriptions:
        sns.subscribe(TopicArn=topic, Protocol='email', Endpoint=email)
    return topic, False



def send(key):
    item = table.get_item(Key=key, ConsistentRead=True).get('Item')
    if not item:
        raise LookupError('Notificación no encontrada')
    prefs = preferences.get_item(Key={'user_id': item['recipient_id']}, ConsistentRead=True).get('Item', {})
    defaults = {'EMAIL': True, 'IN_APP': True}
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
        if channel not in defaults:
            update_channel(key, channel, 'FAILED', error_code='CHANNEL_NOT_SUPPORTED')
            continue
        if not item.get('admin_copy', False) and (not prefs.get('incident_updates', True) or not prefs.get(channel.lower() + '_enabled', defaults[channel])):
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
            topic, confirmed = email_subscription(item['recipient_id'], contact['email'])
            if not confirmed:
                update_channel(key, channel, 'BLOCKED', error_code='SNS_EMAIL_CONFIRMATION_REQUIRED')
                failed = True
                continue
            table.update_item(Key=key, UpdateExpression='SET #d.#c.#s = :sending, #d.#c.lock_until = :expiry, #d.#c.attempts = #d.#c.attempts + :one', ConditionExpression='#d.#c.#s IN (:pending, :retry, :blocked)', ExpressionAttributeNames={'#d': 'delivery_status', '#c': channel, '#s': 'status'}, ExpressionAttributeValues={':sending': 'SENDING', ':expiry': int(time.time()) + 120, ':one': 1, ':pending': 'PENDING', ':retry': 'RETRY', ':blocked': 'BLOCKED'})
        except ClientError as exc:
            if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
                failed = True
                continue
            raise
        except (ConnectionError, LookupError):
            failed = True
            continue
        try:
            result = sns.publish(TopicArn=topic, Subject=item['title'][:100],
                                 Message=item['message'])
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
