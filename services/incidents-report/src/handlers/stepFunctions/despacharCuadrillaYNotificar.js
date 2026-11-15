
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

exports.handler = async event => {
  const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: identifier(event.incidenteId) });
  if (!incident) throw new Error('IncidentNotFound');
  if (['SOLUCIONADA', 'CERRADA'].includes(incident.estado)) return { ...event, dispatch_status: 'ALREADY_RESOLVED' };
  const priority = event.diagnosticoIA.riesgo_estructural === 'ALTO' || ['HIGH', 'CRITICAL'].includes(event.impact?.criticality) ? 'HIGH' : 'MEDIUM';
  await putOutbox({ event_id: 'DISPATCH-' + hash(incident.incidenteId), legacy_detail: true,
    source: 'redtelecorp.incident-management', event_type: 'IncidentNeedsTechnician',
    detail: { incidentId: incident.incidenteId, zoneId: incident.zona,
      requiredSkill: process.env.REQUIRED_FIELD_SKILL || 'NETWORK', description: event.diagnosticoIA.causa_raiz_probable.slice(0, 1500), priority },
    expires_at: Math.floor(Date.now() / 1000) + 2592000 });
  return { ...event, dispatch_status: 'REQUESTED', notification_status: 'QUEUED_BY_OUTBOX' };
};
