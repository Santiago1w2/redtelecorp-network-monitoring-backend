const crypto = require('crypto');
const now = () => new Date().toISOString();
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const { SQSClient, SendMessageCommand } = require('@aws-sdk/client-sqs');
const sqs = new SQSClient({});
exports.handler = async event => {
  const attempt = Number(event.attempt || 0) + 1;
  const envelope = { event_id: 'RETRY-' + hash(event.incidenteId + ':' + attempt), event_type: 'incident.orchestration.retry',
    source: 'incident-management', version: '1.0', occurred_at: now(),
    data: { incident_id: event.incidenteId, zone_id: event.zona, attempt, trigger_reporte_id: event.trigger_reporte_id, problem_type: event.tipoProblema } };
  await sqs.send(new SendMessageCommand({ QueueUrl: attempt <= 3 ? process.env.INCIDENT_INPUT_QUEUE_URL : process.env.ORCHESTRATION_DLQ_URL,
    MessageBody: JSON.stringify(envelope), DelaySeconds: attempt <= 3 ? 30 : 0 }));
  return { orchestration_failed: true, retry_queued: attempt <= 3, incident_id: event.incidenteId };
};
