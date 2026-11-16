import importlib.util
import io
import json
from pathlib import Path
import uuid

import boto3
import pytest

ROOT = Path(__file__).resolve().parents[1]


def module(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


@pytest.fixture
def simulator(aws, monkeypatch):
    ids = ['ANT-SIM-' + uuid.uuid4().hex for _ in range(20)]
    monkeypatch.setenv('SIMULATOR_ANTENNA_IDS', ','.join(ids))
    monkeypatch.setenv('SIMULATOR_SAMPLE_SIZE', '20')
    monkeypatch.setenv('INGEST_QUEUE_URL', aws['sqs'].get_queue_url(QueueName='ingest')['QueueUrl'])
    monkeypatch.setenv('STAGE', 'dev')
    return module(ROOT / 'network-telemetry/handlers/simulate_antennas.py', 'antenna_simulator'), ids


def test_twenty_measurements_reach_real_consumer_and_retries_deduplicate(simulator, aws, monkeypatch, load):
    sim, ids = simulator
    checked = []
    monkeypatch.setattr(sim, 'registered_antenna', checked.append)
    consumer = load('process_telemetry')
    event = {'id': 'scheduled-event-001', 'time': '2026-11-15T20:00:00Z'}
    queue = aws['sqs'].get_queue_url(QueueName='ingest')['QueueUrl']
    for _ in range(2):
        assert sim.lambda_handler(event, None)['submitted'] == 20
        bodies = []
        for _ in range(2):
            page = aws['sqs'].receive_message(QueueUrl=queue, MaxNumberOfMessages=10)
            bodies.extend(page.get('Messages', []))
            for message in page.get('Messages', []):
                aws['sqs'].delete_message(QueueUrl=queue, ReceiptHandle=message['ReceiptHandle'])
        assert len(bodies) == 20
        result = consumer.lambda_handler({'Records': [{'messageId': m['MessageId'], 'body': m['Body']} for m in bodies]}, None)
        assert result['batchItemFailures'] == []
    assert set(checked) == set(ids)
    latest = aws['db'].Table('rtc-telemetry-latest-dev').scan()['Items']
    assert len(latest) == 20
    assert {item['antenna_id'] for item in latest} == set(ids)
    assert aws['db'].Table('rtc-telemetry-history-index-dev').scan()['Count'] == 20


def test_random_samples_use_registered_pool_and_all_real_thresholds(simulator, monkeypatch, load):
    sim, ids = simulator
    monkeypatch.setenv('SIMULATOR_SAMPLE_SIZE', '5')
    classify = load('process_telemetry').classify
    seen = set()
    samples = []
    for index in range(60):
        batch = sim.measurements({'id': str(index), 'time': '2026-11-15T20:00:00Z'})
        assert len(batch) == 5
        assert len({item['antenna_id'] for item in batch}) == 5
        assert {item['antenna_id'] for item in batch} <= set(ids)
        samples.append(batch)
        for item in batch:
            value = item['traffic_gb_min']
            state = classify(value)
            seen.add(state)
            assert (state == 'DISABLED' and value == 0 or state == 'DOWN' and 0 < value < 60
                    or state == 'NORMAL' and 60 <= value <= 129 or state == 'SATURATED' and 130 <= value <= 180)
    assert seen == {'DISABLED', 'DOWN', 'NORMAL', 'SATURATED'}
    assert samples[0] != samples[1]
    assert sim.measurements({'id': '0', 'time': '2026-11-15T20:00:00Z'}) == samples[0]


def test_partial_sqs_failure_is_retried_not_acknowledged(simulator, monkeypatch):
    sim, _ = simulator
    monkeypatch.setattr(sim, 'registered_antenna', lambda _: None)
    class PartialQueue:
        def send_message_batch(self, **kwargs):
            return {'Successful': [{'Id': '0'}], 'Failed': [{'Id': '1', 'Code': 'ServiceUnavailable'}]}
    monkeypatch.setattr(sim.boto3, 'client', lambda _: PartialQueue())
    with pytest.raises(RuntimeError, match='partially failed'):
        sim.lambda_handler({'id': 'same-event', 'time': '2026-11-15T20:00:00Z'}, None)


def test_unregistered_antenna_does_not_publish_anything(simulator, aws, monkeypatch):
    sim, _ = simulator
    def reject(_):
        raise ValueError('Demo antenna is not registered')
    monkeypatch.setattr(sim, 'registered_antenna', reject)
    with pytest.raises(ValueError, match='not registered'):
        sim.lambda_handler({}, None)
    queue = aws['sqs'].get_queue_url(QueueName='ingest')['QueueUrl']
    assert 'Messages' not in aws['sqs'].receive_message(QueueUrl=queue)


def test_infrastructure_lookup_is_signed_with_aws_iam(simulator, monkeypatch):
    sim, ids = simulator
    def receive(req, timeout):
        assert req.full_url.endswith('/v1/infrastructure/antennas/' + ids[0])
        assert req.get_header('Authorization').startswith('AWS4-HMAC-SHA256 ')
        assert timeout == 5
        return io.BytesIO(json.dumps({'antenna_id': ids[0]}).encode())
    monkeypatch.setattr(sim.request, 'urlopen', receive)
    sim.registered_antenna(ids[0])


def test_non_demo_ids_and_non_dev_stage_are_rejected(simulator, monkeypatch):
    sim, ids = simulator
    monkeypatch.setenv('STAGE', 'prod')
    with pytest.raises(ValueError, match='restricted to dev'):
        sim.settings()
    monkeypatch.setenv('STAGE', 'dev')
    monkeypatch.setenv('SIMULATOR_ANTENNA_IDS', ','.join(['ANT-REAL'] + ids[1:]))
    with pytest.raises(ValueError, match='Only ANT-SIM'):
        sim.settings()


def test_manifest_is_random_once_and_stable_on_resume(tmp_path):
    prepare = module(ROOT / 'scripts/prepare_antenna_simulator.py', 'prepare_simulator')
    path = tmp_path / 'manifest.json'
    first = prepare.manifest(path, 'https://example.test', 'ZONE-DEMO', -12.1, -77.0)
    assert len(set(first['antenna_ids'])) == 20
    assert first == prepare.manifest(path, 'https://example.test', 'ZONE-DEMO', -12.1, -77.0)
    other = prepare.manifest(tmp_path / 'other.json', 'https://example.test', 'ZONE-DEMO', -12.1, -77.0)
    assert set(first['antenna_ids']).isdisjoint(other['antenna_ids'])
    with pytest.raises(ValueError, match='another configuration'):
        prepare.manifest(path, 'https://other.test', 'ZONE-DEMO', -12.1, -77.0)
