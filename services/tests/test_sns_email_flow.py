"""Local routing/state checks with Moto DynamoDB and SNS SDK contract stubs."""
import hashlib
from unittest.mock import Mock
import pytest
from botocore.stub import Stubber
import boto3
from conftest import envelope


def event():
    return envelope('incident-management', 'incident.resolved', {
        'incident_id': 'INC-1', 'zone_name': 'Zona Norte', 'incident_status': 'SOLUCIONADA'})


def test_reporters_and_all_admins_receive_once(aws, load, monkeypatch):
    consumer = load('consume_incident_events')
    monkeypatch.setattr(consumer, 'administrator_ids', lambda: {'ADMIN-1', 'ADMIN-2'})
    monkeypatch.setattr(consumer, 'external_json', lambda *a: {'recipient_ids': ['USR-001', 'ADMIN-1']})
    assert consumer.lambda_handler(event(), None)['created_or_deduplicated'] == 3
    consumer.lambda_handler(event(), None)
    items = aws['db'].Table('rtc-notifications-dev').scan()['Items']
    assert len(items) == 3
    assert {i['recipient_id'] for i in items} == {'USR-001', 'ADMIN-1', 'ADMIN-2'}
    assert all(i['channels'] == ['EMAIL', 'IN_APP'] for i in items)
    assert all(i['admin_copy'] == i['recipient_id'].startswith('ADMIN') for i in items)


def test_disabled_cognito_admin_is_excluded(aws, load, monkeypatch):
    consumer = load('consume_incident_events')
    monkeypatch.setenv('COGNITO_USER_POOL_ARN', 'arn:aws:cognito-idp:us-east-1:123456789012:userpool/us-east-1_Test')
    import importlib
    consumer = importlib.reload(consumer)
    client = boto3.client('cognito-idp')
    def user(sub, enabled=True):
        return {'Username': sub, 'Enabled': enabled, 'UserStatus': 'CONFIRMED', 'Attributes': [
            {'Name': 'sub', 'Value': sub}, {'Name': 'email_verified', 'Value': 'true'}]}
    with Stubber(client) as stub:
        stub.add_response('list_users_in_group', {'Users': [user('ADMIN-1')], 'NextToken': 'next'},
                          {'UserPoolId': 'us-east-1_Test', 'GroupName': 'admin', 'Limit': 60})
        stub.add_response('list_users_in_group', {'Users': [user('ADMIN-2', False)]},
                          {'UserPoolId': 'us-east-1_Test', 'GroupName': 'admin', 'Limit': 60, 'NextToken': 'next'})
        monkeypatch.setattr(consumer.boto3, 'client', lambda name: client)
        assert consumer.administrator_ids() == {'ADMIN-1'}
        stub.assert_no_pending_responses()


def test_pending_email_is_retried_without_publishing(aws, load, monkeypatch):
    consumer, sender = load('consume_incident_events'), load('send_notification')
    consumer.create_notification({'recipient_id': 'USR-001', 'incident_id': 'INC-1',
        'template': 'INCIDENT_RESOLVED', 'channels': ['EMAIL', 'IN_APP'],
        'variables': {'zone_name': 'Norte', 'incident_status': 'SOLUCIONADA'}}, 'test-pending')
    item = aws['db'].Table('rtc-notifications-dev').scan()['Items'][0]
    key = {k: item[k] for k in ('recipient_id', 'notification_key')}
    monkeypatch.setenv('DELIVERY_ENABLED', 'true')
    monkeypatch.setattr(sender, 'recipient', lambda user: {'email': 'user@example.com'})
    monkeypatch.setattr(sender, 'email_subscription', lambda *a: ('topic', False))
    publish = Mock()
    monkeypatch.setattr(sender.sns, 'publish', publish)
    with pytest.raises(RuntimeError):
        sender.send(key)
    stored = sender.table.get_item(Key=key)['Item']
    assert stored['delivery_status']['EMAIL']['error_code'] == 'SNS_EMAIL_CONFIRMATION_REQUIRED'
    assert stored['delivery_status']['IN_APP']['status'] == 'DELIVERED'
    publish.assert_not_called()


def test_admin_email_ignores_optout_and_retry_does_not_duplicate(aws, load, monkeypatch):
    consumer, sender = load('consume_incident_events'), load('send_notification')
    consumer.create_notification({'recipient_id': 'ADMIN-1', 'incident_id': 'INC-1',
        'template': 'INCIDENT_RESOLVED', 'channels': ['EMAIL', 'IN_APP'],
        'variables': {'zone_name': 'Norte', 'incident_status': 'SOLUCIONADA'}}, 'test-admin', admin_copy=True)
    item = sender.table.scan()['Items'][0]
    key = {k: item[k] for k in ('recipient_id', 'notification_key')}
    sender.preferences.put_item(Item={'user_id': 'ADMIN-1', 'incident_updates': False, 'email_enabled': False})
    monkeypatch.setenv('DELIVERY_ENABLED', 'true')
    monkeypatch.setattr(sender, 'recipient', lambda user: {'email': 'admin@example.com'})
    topic = 'arn:aws:sns:us-east-1:123456789012:personal-admin'
    monkeypatch.setattr(sender, 'email_subscription', lambda *a: (topic, True))
    with Stubber(sender.sns) as stub:
        stub.add_response('publish', {'MessageId': 'message-1'}, {'TopicArn': topic,
            'Subject': item['title'], 'Message': item['message']})
        sender.send(key)
        sender.send(key)
        stub.assert_no_pending_responses()
    assert sender.table.get_item(Key=key)['Item']['delivery_status']['EMAIL']['status'] == 'SENT'


def test_personal_topic_does_not_publish_to_foreign_email(aws, load):
    sender = load('send_notification')
    email = 'user@example.com'
    suffix = hashlib.sha256(('USR-001' + '\0' + email).encode()).hexdigest()[:32]
    name = 'rtc-notifications-email-dev-' + suffix
    topic = 'arn:aws:sns:us-east-1:123456789012:' + name
    with Stubber(sender.sns) as stub:
        stub.add_response('create_topic', {'TopicArn': topic}, {'Name': name})
        stub.add_response('list_subscriptions_by_topic', {'Subscriptions': [{
            'SubscriptionArn': topic + ':subscription', 'Protocol': 'email',
            'Endpoint': 'other@example.com', 'TopicArn': topic, 'Owner': '123456789012'}]}, {'TopicArn': topic})
        with pytest.raises(PermissionError):
            sender.email_subscription('USR-001', email)
        stub.assert_no_pending_responses()


def test_no_ses_resources_and_no_sms_requests(aws, load, api):
    import yaml
    from pathlib import Path
    config = yaml.safe_load((Path(__file__).parents[1] / 'notifications/serverless.yml').read_text())
    assert not any(r['Type'].startswith('AWS::SES::') for r in config['resources']['Resources'].values())
    handler = load('post_notification')
    data = {'recipient_id': 'USR-001', 'incident_id': 'INC-1', 'template': 'INCIDENT_RESOLVED',
            'channels': ['SMS'], 'variables': {'zone_name': 'Norte', 'incident_status': 'SOLUCIONADA'}}
    assert handler.lambda_handler(api(data), None)['statusCode'] == 400


def test_manual_notification_also_copies_all_admins(aws, load, api, monkeypatch):
    import json
    handler = load('post_notification')
    monkeypatch.setattr(handler, 'administrator_ids', lambda: {'ADMIN-1'})
    monkeypatch.setattr(handler, 'recipient', lambda user: {'user_id': user, 'active': True})
    data = {'recipient_id': 'USR-001', 'incident_id': 'INC-1', 'template': 'INCIDENT_RESOLVED',
            'channels': ['IN_APP'], 'variables': {'zone_name': 'Norte', 'incident_status': 'SOLUCIONADA'}}
    first = handler.lambda_handler(api(data), None)
    assert first['statusCode'] == 202
    assert handler.lambda_handler(api(data), None)['body'] == first['body']
    items = aws['db'].Table('rtc-notifications-dev').scan()['Items']
    assert len(items) == 2
    admin = next(i for i in items if i['recipient_id'] == 'ADMIN-1')
    assert admin['admin_copy'] and admin['channels'] == ['EMAIL', 'IN_APP']
