
const crypto = require('crypto');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand, PutCommand, UpdateCommand, DeleteCommand, QueryCommand, TransactWriteCommand } = require('@aws-sdk/lib-dynamodb');
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), { marshallOptions: { removeUndefinedValues: true } });
const now = () => new Date().toISOString();
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const identifier = value => { if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) throw new Error('InvalidIdentifier'); return value; };
async function get(TableName, Key) { return (await ddb.send(new GetCommand({ TableName, Key, ConsistentRead: true }))).Item; }
async function reportsInZone(zone) {
  const items = []; let key;
  do { const page = await ddb.send(new QueryCommand({ TableName: process.env.REPORTES_TABLE, IndexName: 'ZonaCreatedAtIndex',
    KeyConditionExpression: 'zona = :z AND createdAt >= :t', ExpressionAttributeValues: { ':z': zone, ':t': new Date(Date.now() - 86400000).toISOString() }, ExclusiveStartKey: key }));
    items.push(...(page.Items || [])); key = page.LastEvaluatedKey;
    if (items.length > 10000) throw new Error('ZoneReportLimitExceeded');
  } while (key); return items;
}
async function outbox(type, incident, cause, extra = {}) {
  const event_id = 'EVT-' + hash(incident.incidenteId + ':' + type + ':' + cause);
  const envelope = { event_id, event_type: type, source: 'incident-management', occurred_at: now(), version: '1.0',
    data: { incident_id: incident.incidenteId, zone_id: incident.zona, zone_name: incident.zone_name || incident.zona,
      incident_status: incident.estado, ...extra } };
  return { event_id, envelope, report_ids: incident.reporteIds || [], expires_at: Math.floor(Date.now() / 1000) + 2592000 };
}
async function putOutbox(item) {
  try { await ddb.send(new PutCommand({ TableName: process.env.OUTBOX_TABLE, Item: item, ConditionExpression: 'attribute_not_exists(event_id)' })); }
  catch (error) { if (error.name !== 'ConditionalCheckFailedException') throw error; }
}
async function linkRecipients(incident, users) {
  const unique = [...new Set(users)].filter(Boolean);
  for (let offset = 0; offset < unique.length; offset += 100) await httpJson(process.env.REPORTS_API_URL,
    '/v1/reports/incidents/' + encodeURIComponent(incident) + '/recipients', 'PUT', { recipient_ids: unique.slice(offset, offset + 100) });
}
async function zoneAntennas(zone) {
  let cursor; const antennas = [];
  do { const page = await httpJson(process.env.COVERAGE_API_URL, '/v1/internal/coverage/zones/' + encodeURIComponent(zone) + '/antennas?limit=100' + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
    antennas.push(...page.antennas); cursor = page.cursor; if (antennas.length > 30) throw new Error('ZoneAntennaLimit30');
  } while (cursor);
  for (const antenna of antennas) {
    try { const latest = await httpJson(process.env.TELEMETRY_API_URL, '/v1/internal/telemetry/antennas/' + encodeURIComponent(antenna.antenna_id) + '/latest');
      antenna.traffic_gb_min = Number(latest.traffic_gb_min); antenna.status = latest.status; antenna.timestamp = latest.timestamp;
    } catch (error) { if (error.statusCode !== 404) throw error; antenna.status = 'UNKNOWN'; }
  }
  return antennas;
}

const { SQSClient, SendMessageCommand } = require('@aws-sdk/client-sqs');
const sqs = new SQSClient({});
exports.handler = async event => {
  try {
    const userId = event.requestContext?.authorizer?.claims?.sub;
    if (!userId) return { statusCode: 401, body: JSON.stringify({ message: 'Autenticacion requerida' }) };
    const body = JSON.parse(event.body || '{}');
    const zone = identifier(body.zone_id || body.zona);
    const description = body.description || body.descripcion;
    if (typeof description !== 'string' || !description.trim() || description.length > 1500) throw new Error('InvalidDescription');
    const idempotency = Object.entries(event.headers || {}).find(([key]) => key.toLowerCase() === 'idempotency-key')?.[1];
    const reporteId = 'REP-' + (idempotency ? hash(userId + ':' + idempotency).slice(0, 32) : crypto.randomUUID());
    const createdAt = now();
    const item = { reporteId, userId, zona: zone, descripcion: description, createdAt, estado: 'REGISTRADA' };
    try { await ddb.send(new PutCommand({ TableName: process.env.REPORTES_TABLE, Item: item, ConditionExpression: 'attribute_not_exists(reporteId)' })); }
    catch (error) { if (error.name !== 'ConditionalCheckFailedException') throw error;
      const existing = await get(process.env.REPORTES_TABLE, { reporteId });
      if (existing.zona !== zone || existing.descripcion !== description) return { statusCode: 409, body: JSON.stringify({ message: 'Idempotency-Key reutilizada' }) };
    }
    const envelope = { event_id: reporteId, event_type: 'report.created', source: 'incident-management', occurred_at: createdAt, version: '1.0',
      data: { report_id: reporteId, user_id: userId, zone_id: zone, description, reported_at: createdAt } };
    await sqs.send(new SendMessageCommand({ QueueUrl: process.env.INCIDENT_INPUT_QUEUE_URL, MessageBody: JSON.stringify(envelope) }));
    return { statusCode: 202, body: JSON.stringify({ message: 'Reporte encolado', reporteId }) };
  } catch (error) {
    return { statusCode: error.name === 'SyntaxError' || error.message.startsWith('Invalid') ? 400 : 503, body: JSON.stringify({ message: 'No se pudo registrar el reporte' }) };
  }
};
