
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

const { SignatureV4 } = require('@smithy/signature-v4');
const { HttpRequest } = require('@smithy/protocol-http');
const { Sha256 } = require('@aws-crypto/sha256-js');
const { defaultProvider } = require('@aws-sdk/credential-provider-node');
const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager');
const secrets = new SecretsManagerClient({});
let cachedKey;
async function httpJson(base, route, method = 'GET', data, ai = false) {
  if (!base) throw new Error('IntegrationUrlMissing');
  const url = new URL(base.replace(/\/$/, '') + route);
  if (url.protocol !== 'https:' && !(process.env.LOCAL_TEST === 'true' && ['127.0.0.1', 'localhost'].includes(url.hostname))) throw new Error('HTTPSRequired');
  const body = data === undefined ? undefined : JSON.stringify(data);
  let headers = { 'content-type': 'application/json', host: url.host };
  if (ai) {
    if (!cachedKey) {
      if (!process.env.GCP_AI_SECRET_ARN) throw new Error('AISecretMissing');
      const value = await secrets.send(new GetSecretValueCommand({ SecretId: process.env.GCP_AI_SECRET_ARN }));
      cachedKey = JSON.parse(value.SecretString).api_key;
      if (typeof cachedKey !== 'string' || !cachedKey) throw new Error('AISecretInvalid');
    }
    headers['x-api-key'] = cachedKey;
  } else {
    const signer = new SignatureV4({ credentials: defaultProvider(), region: process.env.AWS_REGION || 'us-east-1', service: 'execute-api', sha256: Sha256 });
    headers = (await signer.sign(new HttpRequest({ protocol: url.protocol, hostname: url.hostname,
      port: url.port ? Number(url.port) : undefined, method, path: url.pathname + url.search, headers, body }))).headers;
  }
  const response = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(ai ? 15000 : 12000) });
  if (!response.ok) { if (ai && response.status === 401) cachedKey = undefined; const error = new Error('IntegrationHTTP' + response.status); error.statusCode = response.status; throw error; }
  return response.json();
}

exports.handler = async event => {
  const zone = identifier(event.zona);
  const reports = await reportsInZone(zone);
  if (event.trigger_reporte_id && !reports.some(report => report.reporteId === event.trigger_reporte_id)) {
    const report = await get(process.env.REPORTES_TABLE, { reporteId: identifier(event.trigger_reporte_id) });
    if (report) reports.push(report);
  }
  const antennas = await zoneAntennas(zone);
  const antenna_ids = antennas.map(antenna => identifier(antenna.antenna_id));
  const impact = antenna_ids.length ? await httpJson(process.env.COVERAGE_API_URL, '/v1/internal/coverage/impact-analysis', 'POST',
    { incident_id: identifier(event.incidenteId), antenna_ids }) : { affected_zones: [], criticality: 'NONE', analysis_method: 'NO_ANTENNAS_AVAILABLE' };
  return { ...event, zona: zone, antenna_ids, antenasNormalizadas: antennas,
    reporteIds: reports.map(report => report.reporteId), recipient_ids: [...new Set(reports.map(report => report.userId))],
    usuariosReportantes: new Set(reports.map(report => report.userId)).size,
    descripciones: reports.slice(-15).map(report => report.descripcion), impact };
};
