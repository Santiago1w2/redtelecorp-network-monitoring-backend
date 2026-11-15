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

const { SFNClient, StartExecutionCommand } = require('@aws-sdk/client-sfn');
const sfn = new SFNClient({});
const ALLOWED = {
  'network-telemetry': ['telemetry.status.changed', 'telemetry.threshold.exceeded'],
  'customer-reports': ['report.created', 'report.status.changed'],
  'field-operations': ['work_order.assigned', 'work_order.pending', 'work_order.status.changed'],
  'incident-management': ['report.created', 'incident.orchestration.retry', 'incident.status.requested'],
};

async function transition(incident, status, cause, updates = {}) {
  if (incident.estado === 'SOLUCIONADA' || incident.estado === 'CERRADA') return;
  if (incident.estado === status && Object.entries(updates).every(([key, value]) => JSON.stringify(incident[key]) === JSON.stringify(value))) return;
  const updated = { ...incident, ...updates, estado: status, updatedAt: now(), revision: (incident.revision || 0) + 1 };
  const type = status === 'SOLUCIONADA' ? 'incident.resolved' : 'incident.status.changed';
  const notification = await outbox(type, updated, cause);
  const actions = [
    { Put: { TableName: process.env.INCIDENTES_TABLE, Item: updated, ConditionExpression: 'revision = :r', ExpressionAttributeValues: { ':r': incident.revision || 0 } } },
    { Put: { TableName: process.env.OUTBOX_TABLE, Item: notification } },
  ];
  if (status === 'SOLUCIONADA') actions.push({ Update: { TableName: process.env.STATE_TABLE, Key: { state_key: 'ZONE#' + incident.zona },
    UpdateExpression: 'SET phase = :p REMOVE active_incident', ConditionExpression: 'active_incident = :i', ExpressionAttributeValues: { ':i': incident.incidenteId, ':p': 'IDLE' } } });
  await ddb.send(new TransactWriteCommand({ TransactItems: actions }));
}

async function maybeResolve(incident, cause, overrides = {}) {
  const antennas = await zoneAntennas(incident.zona);
  const state = { ...incident, ...overrides };
  const normal = antennas.length > 0 ? antennas.every(antenna => antenna.status === 'NORMAL') : state.work_order_status === 'COMPLETED';
  const workDone = !state.work_order_required || state.work_order_status === 'COMPLETED';
  if (normal && workDone) { await transition(incident, 'SOLUCIONADA', cause, { ...overrides, antenasAfectadas: antennas, resolvedAt: now(), tiempoResolucionMinutos: Math.round((Date.now() - Date.parse(incident.createdAt)) / 60000) }); return true; }
  return false;
}

async function startWorkflow(zone, cause, antennas, retry = 0, triggerReport) {
  let state = await get(process.env.STATE_TABLE, { state_key: 'ZONE#' + zone });
  if (state?.active_incident && state.phase !== 'STARTING' && !retry) return state.active_incident;
  if (!state?.active_incident) {
    const generation = (state?.generation || 0) + 1;
    const incidentId = 'INC-' + hash(zone + ':' + generation).slice(0, 32);
    try { await ddb.send(new PutCommand({ TableName: process.env.STATE_TABLE,
      Item: { state_key: 'ZONE#' + zone, generation, active_incident: incidentId, phase: 'STARTING' },
      ConditionExpression: 'attribute_not_exists(active_incident)' })); }
    catch (error) { if (error.name !== 'ConditionalCheckFailedException') throw error; }
    state = await get(process.env.STATE_TABLE, { state_key: 'ZONE#' + zone });
  }
  const input = { zona: zone, incidenteId: state.active_incident, antenna_ids: antennas.map(antenna => antenna.antenna_id),
    antenas_estado: antennas, cause_event_id: cause, attempt: retry, trigger_reporte_id: triggerReport };
  try { await sfn.send(new StartExecutionCommand({ stateMachineArn: process.env.STATE_MACHINE_ARN,
    name: state.active_incident + (retry ? '-retry-' + retry : ''), input: JSON.stringify(input) })); }
  catch (error) { if (error.name !== 'ExecutionAlreadyExists') throw error; }
  return state.active_incident;
}

async function processEnvelope(envelope) {
  const data = envelope.data;
  if (envelope.event_type === 'incident.orchestration.retry') {
    await startWorkflow(identifier(data.zone_id), envelope.event_id, await zoneAntennas(data.zone_id), data.attempt, data.trigger_reporte_id); return;
  }
  if (envelope.event_type === 'incident.status.requested') {
    const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: identifier(data.incident_id) });
    if (!incident) throw new Error('IncidentNotFound');
    if (data.status === 'SOLUCIONADA') {
      if (!await maybeResolve(incident, envelope.event_id)) throw new Error('RecoveryNotVerified');
    } else if (['EN_REVISION', 'EN_ATENCION', 'SIENDO_ATENDIDA'].includes(data.status)) {
      await transition(incident, data.status, envelope.event_id);
    } else throw new Error('InvalidIncidentTransition');
    return;
  }
  if (envelope.source === 'field-operations') {
    const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: identifier(data.incident_id) });
    if (!incident) throw new Error('IncidentNotFound');
    if (incident.work_order_status === 'COMPLETED' && data.status !== 'COMPLETED') return;
    if (incident.work_order_timestamp && incident.work_order_timestamp > envelope.occurred_at) return;
    const updates = { work_order_id: identifier(data.work_order_id), work_order_status: data.status,
      work_order_required: true, work_order_timestamp: envelope.occurred_at };
    if (await maybeResolve(incident, envelope.event_id, updates)) return;
    await transition(incident, data.status === 'PENDING_ASSIGNMENT' ? 'EN_REVISION' : 'EN_ATENCION', envelope.event_id, updates);
    return;
  }
  let zone;
  if (envelope.source === 'network-telemetry') {
    identifier(data.antenna_id);
    if (!Number.isFinite(Number(data.traffic_gb_min)) || Number(data.traffic_gb_min) < 0) throw new Error('InvalidTraffic');
    const coverage = await httpJson(process.env.COVERAGE_API_URL, '/v1/internal/coverage/antennas/' + encodeURIComponent(data.antenna_id));
    zone = identifier(coverage.zone_id);
  } else {
    zone = identifier(data.zone_id);
    if (envelope.event_type === 'report.status.changed') {
      if (await get(process.env.REPORTES_TABLE, { reporteId: identifier(data.report_id) })) {
        await ddb.send(new UpdateCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId: data.report_id },
          UpdateExpression: 'SET estado = :s', ExpressionAttributeValues: { ':s': data.status } }));
      }
      return;
    }
    let report = data;
    if (!report.description) {
      const response = await httpJson(process.env.REPORTS_API_URL, '/v1/reports/' + encodeURIComponent(identifier(data.report_id)));
      const r = response.report;
      report = { ...data, description: r.description, user_id: r.userId, zone_id: r.zoneId };
    }
    identifier(report.user_id);
    if (typeof report.description !== 'string' || !report.description.trim()) throw new Error('ReportDescriptionMissing');
    const existing = await get(process.env.REPORTES_TABLE, { reporteId: identifier(data.report_id) });
    if (!existing) await ddb.send(new PutCommand({ TableName: process.env.REPORTES_TABLE,
      Item: { reporteId: data.report_id, userId: report.user_id, zona: zone, descripcion: report.description,
        createdAt: report.reported_at || envelope.occurred_at, estado: 'REGISTRADA' } }));
    let ai = existing?.clasificacionIA;
    if (!ai) {
      ai = process.env.AI_ENABLED === 'true'
        ? await httpJson(process.env.GCP_AI_BASE_URL, '/api/v1/ai/prioritize', 'POST', { usuario_id: report.user_id, zona: zone, descripcion: report.description }, true)
        : { prioridad: 'MEDIA', es_infraestructura_critica: false, analysis_method: 'RULES_AI_DISABLED' };
      await ddb.send(new UpdateCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId: data.report_id },
        UpdateExpression: 'SET clasificacionIA = :ai, estado = :s', ExpressionAttributeValues: { ':ai': ai, ':s': 'EN_REVISION' } }));
    }
    const state = await get(process.env.STATE_TABLE, { state_key: 'ZONE#' + zone });
    if (state?.active_incident) {
      await linkRecipients(state.active_incident, [report.user_id]);
      const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: state.active_incident });
      if (incident) await putOutbox(await outbox('incident.collective.detected', incident, envelope.event_id, { recipient_ids: [report.user_id] }));
      return;
    }
    const reports = await reportsInZone(zone);
    if (!reports.some(item => item.reporteId === data.report_id)) reports.push({ reporteId: data.report_id, userId: report.user_id });
    const antennas = await zoneAntennas(zone);
    if (new Set(reports.map(item => item.userId)).size >= Number(process.env.COLLECTIVE_REPORT_THRESHOLD || 10)
      || ai.es_infraestructura_critica === true || antennas.some(antenna => ['DOWN', 'DISABLED', 'SATURATED'].includes(antenna.status))) {
      await startWorkflow(zone, envelope.event_id, antennas, 0, data.report_id);
    }
    return;
  }
  const antennas = await zoneAntennas(zone);
  const state = await get(process.env.STATE_TABLE, { state_key: 'ZONE#' + zone });
  if (state?.active_incident) {
    const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: state.active_incident });
    if (incident) await maybeResolve(incident, envelope.event_id);
  } else if (antennas.some(antenna => ['DOWN', 'DISABLED', 'SATURATED'].includes(antenna.status))) await startWorkflow(zone, envelope.event_id, antennas);
}

exports.handler = async (event) => {
  const failures = [];
  for (const record of event.Records || []) {
    let key; const owner = crypto.randomUUID();
    try {
      const outer = JSON.parse(record.body); const envelope = outer.detail || outer;
      if (envelope.version !== '1.0' || !ALLOWED[envelope.source]?.includes(envelope.event_type) || !envelope.data
        || (outer['detail-type'] && outer['detail-type'] !== envelope.event_type) || (outer.source && outer.source !== envelope.source)) throw new Error('InvalidEventEnvelope');
      identifier(envelope.event_id);
      if (!Number.isFinite(Date.parse(envelope.occurred_at))) throw new Error('InvalidEventTimestamp');
      key = { state_key: 'EVENT#' + hash(envelope.source + ':' + envelope.event_type + ':' + envelope.event_id) };
      const saved = await get(process.env.STATE_TABLE, key);
      if (saved?.status === 'COMPLETED') continue;
      try { await ddb.send(new PutCommand({ TableName: process.env.STATE_TABLE,
        Item: { ...key, owner, status: 'PROCESSING', lock_until: Math.floor(Date.now() / 1000) + 120, expires_at: Math.floor(Date.now() / 1000) + 2592000 },
        ConditionExpression: 'attribute_not_exists(state_key) OR lock_until < :t', ExpressionAttributeValues: { ':t': Math.floor(Date.now() / 1000) } })); }
      catch (error) { if (error.name === 'ConditionalCheckFailedException') throw new Error('ConcurrentEventProcessing'); throw error; }
      await processEnvelope(envelope);
      await ddb.send(new UpdateCommand({ TableName: process.env.STATE_TABLE, Key: key, UpdateExpression: 'SET #s = :s REMOVE lock_until',
        ConditionExpression: '#o = :o', ExpressionAttributeNames: { '#s': 'status', '#o': 'owner' }, ExpressionAttributeValues: { ':s': 'COMPLETED', ':o': owner } }));
    } catch (error) {
      if (key) try { await ddb.send(new DeleteCommand({ TableName: process.env.STATE_TABLE, Key: key, ConditionExpression: '#o = :o', ExpressionAttributeNames: { '#o': 'owner' }, ExpressionAttributeValues: { ':o': owner } })); } catch (_) {}
      console.error('incident_event_failed', error.name);
      failures.push({ itemIdentifier: record.messageId });
    }
  }
  return { batchItemFailures: failures };
};
