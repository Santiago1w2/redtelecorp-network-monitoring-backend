const crypto = require('crypto');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand, PutCommand, UpdateCommand, DeleteCommand, QueryCommand, TransactWriteCommand } = require('@aws-sdk/lib-dynamodb');
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), { marshallOptions: { removeUndefinedValues: true } });
const now = () => new Date().toISOString();
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const identifier = value => { if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) throw new Error('InvalidIdentifier'); return value; };
async function get(TableName, Key) { return (await ddb.send(new GetCommand({ TableName, Key, ConsistentRead: true }))).Item; }
function audit(id, at, eventId, previous, next, actor = 'INCIDENT-MANAGEMENT', comment = '') {
  return { entidadId: id, timestamp: at + '#' + eventId, occurred_at: at, estadoAnterior: previous || null, estadoNuevo: next, actorId: actor, comentario: comment };
}

async function reportsInZone(zone, problemType) {
  const items = []; let key;
  do { const page = await ddb.send(new QueryCommand({ TableName: process.env.REPORTES_TABLE, IndexName: 'ZonaCreatedAtIndex',
    KeyConditionExpression: 'zona = :z AND createdAt BETWEEN :from AND :to',
    FilterExpression: problemType ? 'tipoProblema = :type' : undefined,
    ExpressionAttributeValues: { ':z': zone, ':from': new Date(Date.now() - 86400000).toISOString(), ':to': now(), ...(problemType ? { ':type': problemType } : {}) }, ExclusiveStartKey: key }));
    items.push(...(page.Items || [])); key = page.LastEvaluatedKey;
    if (items.length > 1000) throw new Error('ZoneReportLimit1000');
  } while (key); return items;
}
async function outbox(type, incident, cause, extra = {}) {
  const event_id = 'EVT-' + hash(incident.incidenteId + ':' + type + ':' + cause);
  const envelope = { event_id, event_type: type, source: 'incident-management', occurred_at: now(), version: '1.0',
    data: { incident_id: incident.incidenteId, zone_id: incident.zona, zone_name: incident.zone_name || incident.zona, incident_status: incident.estado, ...extra } };
  return { event_id, envelope, report_ids: incident.reporteIds || [], expires_at: Math.floor(Date.now() / 1000) + 2592000 };
}
async function putOutbox(item) {
  try { await ddb.send(new PutCommand({ TableName: process.env.OUTBOX_TABLE, Item: item, ConditionExpression: 'attribute_not_exists(event_id)' })); }
  catch (error) { if (error.name !== 'ConditionalCheckFailedException') throw error; }
}
async function linkRecipients(incident, users) {
  const unique = [...new Set(users)].filter(Boolean);
  for (let offset = 0; offset < unique.length; offset += 10) await Promise.all(unique.slice(offset, offset + 10).map(user => ddb.send(new PutCommand({
    TableName: process.env.RECIPIENTS_TABLE, Item: { incident_id: incident, recipient_id: identifier(user) } }))));
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
let cachedKey, keyExpires = 0, cachedIdToken, idExpires = 0;
async function aiKey() {
  if (cachedKey && Date.now() < keyExpires) return cachedKey;
  if (process.env.GCP_AI_SECRET_ARN) {
    const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager');
    const value = await new SecretsManagerClient({}).send(new GetSecretValueCommand({ SecretId: process.env.GCP_AI_SECRET_ARN }));
    cachedKey = JSON.parse(value.SecretString).api_key;
  } else {
    if (!process.env.GCP_AI_API_KEY_PARAM) throw new Error('AISecretMissing');
    const { SSMClient, GetParameterCommand } = require('@aws-sdk/client-ssm');
    const value = await new SSMClient({}).send(new GetParameterCommand({ Name: process.env.GCP_AI_API_KEY_PARAM, WithDecryption: true }));
    cachedKey = value.Parameter?.Value;
  }
  if (typeof cachedKey !== 'string' || !cachedKey) throw new Error('AISecretInvalid');
  keyExpires = Date.now() + 300000;
  return cachedKey;
}
async function cloudRunToken(audience) {
  if (cachedIdToken && Date.now() < idExpires) return cachedIdToken;
  const providerAudience = process.env.GCP_WIF_AUDIENCE;
  const account = process.env.GCP_INVOKER_SERVICE_ACCOUNT;
  if (!/^\/\/iam\.googleapis\.com\/projects\/\d+\/locations\/global\/workloadIdentityPools\/[\w-]+\/providers\/[\w-]+$/.test(providerAudience || '')
    || !/^[\w.-]+@[\w.-]+\.iam\.gserviceaccount\.com$/.test(account || '')) throw new Error('WIFConfigurationMissing');
  const { AwsClient } = require('google-auth-library');
  const auth = new AwsClient({ audience: providerAudience, subject_token_type: 'urn:ietf:params:aws:token-type:aws4_request', token_url: 'https://sts.googleapis.com/v1/token',
    scopes: ['https://www.googleapis.com/auth/cloud-platform'], aws_security_credentials_supplier: {
      getAwsRegion: async () => process.env.AWS_REGION || 'us-east-1',
      getAwsSecurityCredentials: async () => { const value = await defaultProvider()(); return { accessKeyId: value.accessKeyId, secretAccessKey: value.secretAccessKey, token: value.sessionToken }; },
    } });
  const subject = await auth.retrieveSubjectToken();
  const exchange = await fetch('https://sts.googleapis.com/v1/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange', audience: providerAudience,
      requested_token_type: 'urn:ietf:params:oauth:token-type:access_token', subject_token_type: 'urn:ietf:params:aws:token-type:aws4_request',
      subject_token: subject, scope: 'https://www.googleapis.com/auth/cloud-platform' }), signal: AbortSignal.timeout(8000),
  });
  if (!exchange.ok) { const error = new Error('WIFExchangeHTTP' + exchange.status); error.statusCode = [400, 401, 403].includes(exchange.status) ? 403 : exchange.status; throw error; }
  const access = await exchange.json();
  if (typeof access.access_token !== 'string' || !access.access_token) throw new Error('WIFAccessTokenInvalid');
  const response = await fetch('https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' + encodeURIComponent(account) + ':generateIdToken', {
    method: 'POST', headers: { Authorization: 'Bearer ' + access.access_token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ audience, includeEmail: true }), signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) { const error = new Error('WIFTokenHTTP' + response.status); error.statusCode = response.status; throw error; }
  const value = await response.json();
  if (typeof value.token !== 'string' || value.token.split('.').length !== 3) throw new Error('WIFTokenInvalid');
  cachedIdToken = value.token;
  idExpires = Math.min(Date.now() + 3000000, Number(JSON.parse(Buffer.from(value.token.split('.')[1], 'base64url').toString()).exp) * 1000 - 60000);
  return cachedIdToken;
}
async function httpJson(base, route, method = 'GET', data, ai = false) {
  if (!base) throw new Error('IntegrationUrlMissing');
  const url = new URL(base.replace(/\/$/, '') + route);
  if (url.protocol !== 'https:' && !(process.env.LOCAL_TEST === 'true' && ['127.0.0.1', 'localhost'].includes(url.hostname))) throw new Error('HTTPSRequired');
  const body = data === undefined ? undefined : JSON.stringify(data);
  let signedHeaders = { 'content-type': 'application/json', host: url.host };
  if (ai) {
    signedHeaders['x-api-key'] = await aiKey();
    const mode = process.env.GCP_AUTH_MODE || 'WIF';
    if (mode === 'WIF') signedHeaders.Authorization = 'Bearer ' + await cloudRunToken(new URL(base).origin);
    else if (mode !== 'API_KEY') throw new Error('InvalidGCPAuthMode');
  } else {
    const signer = new SignatureV4({ credentials: defaultProvider(), region: process.env.AWS_REGION || 'us-east-1', service: 'execute-api', sha256: Sha256 });
    signedHeaders = (await signer.sign(new HttpRequest({ protocol: url.protocol, hostname: url.hostname,
      port: url.port ? Number(url.port) : undefined, method, path: url.pathname, query: Object.fromEntries(url.searchParams), headers: signedHeaders, body }))).headers;
  }
  const response = await fetch(url, { method, headers: signedHeaders, body, signal: AbortSignal.timeout(ai ? 15000 : 12000) });
  if (!response.ok) { if (ai && [401, 403].includes(response.status)) { cachedKey = undefined; cachedIdToken = undefined; } const error = new Error('IntegrationHTTP' + response.status); error.statusCode = response.status; throw error; }
  return response.json();
}
exports.handler = async event => {
  const zone = identifier(event.zona);
  const reports = await reportsInZone(zone, event.tipoProblema);
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
