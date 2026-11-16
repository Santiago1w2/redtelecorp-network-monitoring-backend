const crypto = require('crypto');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand, TransactWriteCommand } = require('@aws-sdk/lib-dynamodb');

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), { marshallOptions: { removeUndefinedValues: true } });
const BAD_ANTENNA_STATES = new Set(['DOWN', 'DISABLED', 'SATURATED']);
const hash = value => crypto.createHash('sha256').update(value).digest('hex');

function metricFor(envelope) {
  const { event_type: type, source, occurred_at: at, data } = envelope;
  const day = at.slice(0, 10);
  if (source === 'incident-management' && type === 'report.created') {
    return { kind: 'REPORT', day, zone: data.zone_id, dimension: data.problem_type || 'GENERAL', metricId: envelope.event_id };
  }
  if (source === 'network-telemetry' && ['telemetry.status.changed', 'telemetry.threshold.exceeded'].includes(type)
    && BAD_ANTENNA_STATES.has(data.current_status) && !BAD_ANTENNA_STATES.has(data.previous_status)) {
    return { kind: 'ANTENNA', day, zone: '-', dimension: data.antenna_id, status: data.current_status, metricId: envelope.event_id };
  }
  if (source === 'incident-management' && ['incident.collective.detected', 'incident.preventive.detected'].includes(type)) {
    return { kind: 'INCIDENT', day, zone: data.zone_id, dimension: data.criticality || 'UNKNOWN', metricId: data.incident_id };
  }
  if (source === 'incident-management' && type === 'incident.resolved' && Number.isFinite(Number(data.resolution_minutes))) {
    return { kind: 'RESOLUTION', day, zone: data.zone_id || '-', dimension: 'RESOLVED', resolutionMinutes: Number(data.resolution_minutes), metricId: data.incident_id };
  }
  return null;
}

async function projectMetric(envelope) {
  const metric = metricFor(envelope);
  if (!metric || !process.env.METRICS_TABLE) return;
  const period = metric.day.slice(0, 7);
  const metricKey = `DAY#${metric.day}#${metric.kind}#${metric.zone}#${metric.dimension}`;
  const markerKey = { period: 'EVENT#' + metric.kind + '#' + hash(metric.metricId), metric_key: 'PROCESSED' };
  const expiresAt = Math.floor(Date.now() / 1000) + 400 * 86400;
  const update = { TableName: process.env.METRICS_TABLE, Key: { period, metric_key: metricKey },
    UpdateExpression: metric.kind === 'RESOLUTION'
      ? 'SET #kind = :kind, #day = :day, #zone = :zone, #dimension = :dimension, #status = :status, expires_at = :expires ADD event_count :one, resolution_minutes_total :duration'
      : 'SET #kind = :kind, #day = :day, #zone = :zone, #dimension = :dimension, #status = :status, expires_at = :expires ADD event_count :one',
    ExpressionAttributeNames: { '#kind': 'kind', '#day': 'day', '#zone': 'zone', '#dimension': 'dimension', '#status': 'status' },
    ExpressionAttributeValues: { ':kind': metric.kind, ':day': metric.day, ':zone': metric.zone, ':dimension': metric.dimension,
      ':status': metric.status || '-', ':expires': expiresAt, ':one': 1, ...(metric.kind === 'RESOLUTION' ? { ':duration': metric.resolutionMinutes } : {}) } };
  try {
    await ddb.send(new TransactWriteCommand({ TransactItems: [
      { Put: { TableName: process.env.METRICS_TABLE, Item: { ...markerKey, expires_at: expiresAt }, ConditionExpression: 'attribute_not_exists(period)' } },
      { Update: update },
    ] }));
  } catch (error) {
    if (!['TransactionCanceledException', 'ConditionalCheckFailedException'].includes(error.name)) throw error;
    const marker = (await ddb.send(new GetCommand({ TableName: process.env.METRICS_TABLE, Key: markerKey, ConsistentRead: true }))).Item;
    if (!marker) throw error;
  }
}

module.exports = { projectMetric };
