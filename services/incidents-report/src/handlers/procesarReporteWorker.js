const crypto = require('crypto');
const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand, PutCommand, UpdateCommand, DeleteCommand, QueryCommand, TransactWriteCommand } = require('@aws-sdk/lib-dynamodb');
const { projectMetric } = require('../lib/projectMetric');
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), { marshallOptions: { removeUndefinedValues: true } });
const now = () => new Date().toISOString();
const hash = value => crypto.createHash('sha256').update(value).digest('hex');
const identifier = value => { if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) throw new Error('InvalidIdentifier'); return value; };
async function get(TableName, Key) { return (await ddb.send(new GetCommand({ TableName, Key, ConsistentRead: true }))).Item; }
function audit(id, at, eventId, previous, next, actor = 'INCIDENT-MANAGEMENT', comment = '') {
  return { entidadId: id, timestamp: at + '#' + eventId, occurred_at: at, estadoAnterior: previous || null, estadoNuevo: next, actorId: actor, comentario: comment };
}

async function distinctReportersInZone(zone, problemType, requiredCount) {
  const users = new Set(); let key;
  do {
    const page = await ddb.send(new QueryCommand({ TableName: process.env.REPORTES_TABLE, IndexName: 'ZonaCreatedAtIndex',
      KeyConditionExpression: 'zona = :z AND createdAt BETWEEN :from AND :to',
      FilterExpression: problemType ? 'tipoProblema = :type' : undefined,
      ExpressionAttributeValues: { ':z': zone, ':from': new Date(Date.now() - 86400000).toISOString(), ':to': now(), ...(problemType ? { ':type': problemType } : {}) },
      ProjectionExpression: 'userId', Limit: 100, ExclusiveStartKey: key }));
    for (const item of page.Items || []) {
      if (item.userId) users.add(item.userId);
      if (users.size >= requiredCount) return users.size;
    }
    key = page.LastEvaluatedKey;
  } while (key);
  return users.size;
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
const { SFNClient, StartExecutionCommand } = require('@aws-sdk/client-sfn');
const sfn = new SFNClient({});
const ALLOWED = {
  'network-telemetry': ['telemetry.status.changed', 'telemetry.threshold.exceeded'],
  'customer-reports': ['report.created', 'report.status.changed'],
  'field-operations': ['work_order.assigned', 'work_order.pending', 'work_order.status.changed'],
  'incident-management': ['report.created', 'incident.orchestration.retry', 'incident.status.requested'],
};

const BAD_ANTENNA_STATES = new Set(['DOWN', 'DISABLED', 'SATURATED']);

function mergeFaultyAntennas(incident, current = []) {
  const faults = new Map();
  for (const antenna of [...(incident.antenasConFalla || []), ...(incident.antenasAfectadas || []), ...current]) {
    if (antenna?.antenna_id && BAD_ANTENNA_STATES.has(antenna.status) && !faults.has(antenna.antenna_id)) {
      faults.set(antenna.antenna_id, antenna);
    }
  }
  return [...faults.values()];
}

async function transition(incident, status, cause, updates = {}) {
  if (incident.estado === 'CERRADA' || (incident.estado === 'SOLUCIONADA' && status !== 'CERRADA')) return;
  if (incident.estado === status && Object.entries(updates).every(([key, value]) => JSON.stringify(incident[key]) === JSON.stringify(value))) return;
  const updated = { ...incident, ...updates, antenasConFalla: mergeFaultyAntennas(incident, updates.antenasAfectadas), estado: status, updatedAt: now(), revision: (incident.revision || 0) + 1 };
  const type = ['SOLUCIONADA', 'CERRADA'].includes(status) ? 'incident.resolved' : 'incident.status.changed';
  const notification = await outbox(type, updated, cause, {
    ...(Number.isFinite(updates.tiempoResolucionMinutos) ? { resolution_minutes: updates.tiempoResolucionMinutos } : {}),
    ...(updated.impacto?.criticality ? { criticality: updated.impacto.criticality } : {}),
  });
  const actions = [
    { Put: { TableName: process.env.INCIDENTES_TABLE, Item: updated, ConditionExpression: 'revision = :r', ExpressionAttributeValues: { ':r': incident.revision || 0 } } },
    { Put: { TableName: process.env.OUTBOX_TABLE, Item: notification } },
    { Put: { TableName: process.env.HISTORIAL_TABLE, Item: audit(incident.incidenteId, updated.updatedAt, notification.event_id, incident.estado, status) } },
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

async function startWorkflow(zone, cause, antennas, retry = 0, triggerReport, problemType, recurrence = null) {
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
    antenas_estado: antennas, cause_event_id: cause, attempt: retry, trigger_reporte_id: triggerReport, tipoProblema: problemType,
    ...(recurrence ? { recurrencia: recurrence } : {}) };
  try { await sfn.send(new StartExecutionCommand({ stateMachineArn: process.env.STATE_MACHINE_ARN,
    name: state.active_incident + (retry ? '-retry-' + retry : ''), input: JSON.stringify(input) })); }
  catch (error) { if (error.name !== 'ExecutionAlreadyExists') throw error; }
  return state.active_incident;
}

async function recordAntennaFailure(antennaId, envelope) {
  const data = envelope.data;
  if (!BAD_ANTENNA_STATES.has(data.current_status) || BAD_ANTENNA_STATES.has(data.previous_status)) return null;
  const occurredAt = new Date(envelope.occurred_at);
  if (!Number.isFinite(occurredAt.getTime())) throw new Error('InvalidTelemetryTimestamp');
  const configuredWindow = Number(process.env.RECURRENT_ANTENNA_WINDOW_DAYS);
  const windowDays = Number.isInteger(configuredWindow) && configuredWindow >= 2 ? Math.min(31, configuredWindow) : 7;
  const day = occurredAt.toISOString().slice(0, 10);
  const dayKey = `ANTENNA-FAIL#${antennaId}#DAY#${day}`;
  const markerKey = { state_key: `ANTENNA-FAIL#${antennaId}#EVENT#${hash(envelope.event_id)}` };
  const expiresAt = Math.floor(Date.now() / 1000) + (windowDays + 2) * 86400;
  try {
    await ddb.send(new TransactWriteCommand({ TransactItems: [
      { Put: { TableName: process.env.STATE_TABLE, Item: { ...markerKey, expires_at: expiresAt }, ConditionExpression: 'attribute_not_exists(state_key)' } },
      { Update: { TableName: process.env.STATE_TABLE, Key: { state_key: dayKey },
        UpdateExpression: 'SET expires_at = :expires ADD failure_count :one',
        ExpressionAttributeValues: { ':expires': expiresAt, ':one': 1 } } },
    ] }));
  } catch (error) {
    if (!['TransactionCanceledException', 'ConditionalCheckFailedException'].includes(error.name)) throw error;
    if (!await get(process.env.STATE_TABLE, markerKey)) throw error;
  }

  let events = 0;
  const days = [];
  for (let offset = 0; offset < windowDays; offset += 1) {
    const date = new Date(occurredAt.getTime() - offset * 86400000).toISOString().slice(0, 10);
    const daily = await get(process.env.STATE_TABLE, { state_key: `ANTENNA-FAIL#${antennaId}#DAY#${date}` });
    if (daily?.failure_count) {
      events += Number(daily.failure_count);
      days.push(date);
    }
  }
  const configuredEvents = Number(process.env.RECURRENT_ANTENNA_MIN_EVENTS);
  const configuredDays = Number(process.env.RECURRENT_ANTENNA_MIN_DAYS);
  const minEvents = Number.isInteger(configuredEvents) && configuredEvents >= 2 ? configuredEvents : 3;
  const minDays = Number.isInteger(configuredDays) && configuredDays >= 2 ? Math.min(windowDays, configuredDays) : 2;
  if (events < minEvents || days.length < minDays) return null;
  return { antenna_id: antennaId, failure_events: events, affected_days: days.length, window_days: windowDays };
}

async function processEnvelope(envelope) {
  const data = envelope.data;
  if (envelope.event_type === 'incident.orchestration.retry') {
    await startWorkflow(identifier(data.zone_id), envelope.event_id, await zoneAntennas(data.zone_id), data.attempt, data.trigger_reporte_id, data.problem_type); return;
  }

  if (envelope.event_type === 'incident.status.requested') {
    const id = identifier(data.incident_id);
    if (!id.startsWith('INC-')) {
      const report = await get(process.env.REPORTES_TABLE, { reporteId: id });
      if (!report) throw new Error('ReportNotFound');
      if (report.incidenteColectivoId) throw new Error('LinkedReportMustUseIncident');
      if (report.estado === data.status || (data.expected_state && report.estado !== data.expected_state)) return;
      if (data.status === 'SOLUCIONADA') {
        const antennas = await zoneAntennas(report.zona);
        if (!antennas.length || antennas.some(antenna => antenna.status !== 'NORMAL')) throw new Error('RecoveryNotVerified');
      }
      const at = now();
      const notification = await outbox(data.status === 'SOLUCIONADA' ? 'incident.resolved' : 'incident.status.changed', { incidenteId: id, zona: report.zona, estado: data.status }, envelope.event_id);
      await ddb.send(new TransactWriteCommand({ TransactItems: [
        { Update: { TableName: process.env.REPORTES_TABLE, Key: { reporteId: id }, UpdateExpression: 'SET estado = :next, updatedAt = :at', ConditionExpression: 'estado = :old', ExpressionAttributeValues: { ':next': data.status, ':at': at, ':old': report.estado } } },
        { Put: { TableName: process.env.HISTORIAL_TABLE, Item: audit(id, at, envelope.event_id, report.estado, data.status, data.actor_id, data.comment) } },
        { Put: { TableName: process.env.OUTBOX_TABLE, Item: notification } },
      ] }));
      return;
    }
    const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: id });
    if (!incident) throw new Error('IncidentNotFound');
    if (data.expected_state && incident.estado !== data.expected_state) return;
    if (data.status === 'SOLUCIONADA') {
      if (!await maybeResolve(incident, envelope.event_id)) throw new Error('RecoveryNotVerified');
    } else if (data.status === 'CERRADA' && incident.estado === 'SOLUCIONADA') await transition(incident, 'CERRADA', envelope.event_id);
    else if (data.status === 'EN_REVISION') await transition(incident, data.status, envelope.event_id);
    else if (['EN_ATENCION', 'SIENDO_ATENDIDA'].includes(data.status) && ['ASSIGNED', 'IN_PROGRESS', 'COMPLETED'].includes(incident.work_order_status)) await transition(incident, data.status, envelope.event_id);
    else throw new Error('InvalidIncidentTransition');
    return;
  }
  if (envelope.source === 'field-operations') {
    if (!['PENDING_ASSIGNMENT', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'].includes(data.status)) throw new Error('InvalidWorkOrderStatus');
    const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: identifier(data.incident_id) });
    if (!incident) throw new Error('IncidentNotFound');
    if (incident.work_order_status === 'COMPLETED' && data.status !== 'COMPLETED') return;
    if (incident.work_order_timestamp && incident.work_order_timestamp > envelope.occurred_at) return;
    const updates = { work_order_id: identifier(data.work_order_id), work_order_status: data.status,
      work_order_required: true, work_order_timestamp: envelope.occurred_at };
    if (await maybeResolve(incident, envelope.event_id, updates)) return;
    await transition(incident, ['PENDING_ASSIGNMENT', 'CANCELLED'].includes(data.status) ? 'EN_REVISION' : 'EN_ATENCION', envelope.event_id, updates);
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
      const report = await get(process.env.REPORTES_TABLE, { reporteId: identifier(data.report_id) });
      // Los callbacks del legado no sobrescriben un estado gobernado por el incidente canónico.
      const status = { REGISTERED: 'REGISTRADA', IN_REVIEW: 'EN_REVISION', IN_PROGRESS: 'SIENDO_ATENDIDA', RESOLVED: 'SOLUCIONADA' }[data.status];
      if (report?.legacy_source && !report.incidenteColectivoId && status && (!report.legacy_updated_at || report.legacy_updated_at < envelope.occurred_at)) {
        await ddb.send(new UpdateCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId: data.report_id },
          UpdateExpression: 'SET estado = :s, legacy_updated_at = :at', ExpressionAttributeValues: { ':s': status, ':at': envelope.occurred_at } }));
      }
      return;
    }
    let report = data;
    const authoritative = envelope.source === 'incident-management' ? await get(process.env.REPORTES_TABLE, { reporteId: identifier(data.report_id) }) : null;
    if (authoritative) report = { ...data, description: authoritative.descripcion, user_id: authoritative.userId, problem_type: authoritative.tipoProblema || 'GENERAL', imagen_base64: authoritative.imagen_base64, imagen_mime_type: authoritative.imagen_mime_type };
    if (envelope.source === 'incident-management' && !authoritative) throw new Error('ReportNotFound');
    if (!report.description) {
      const response = await httpJson(process.env.LEGACY_REPORTS_API_URL, '/v1/reports/' + encodeURIComponent(identifier(data.report_id)));
      const r = response.report;
      report = { ...data, description: r.description, user_id: r.userId, zone_id: r.zoneId };
    }
    identifier(report.user_id);
    if (typeof report.description !== 'string' || !report.description.trim()) throw new Error('ReportDescriptionMissing');
    const existing = await get(process.env.REPORTES_TABLE, { reporteId: identifier(data.report_id) });
    if (!existing) await ddb.send(new PutCommand({ TableName: process.env.REPORTES_TABLE,
      Item: { reporteId: data.report_id, userId: report.user_id, zona: zone, descripcion: report.description,
        createdAt: report.reported_at || envelope.occurred_at, estado: 'REGISTRADA', tipoProblema: report.problem_type || 'GENERAL', legacy_source: envelope.source === 'customer-reports' } }));
    let ai = existing?.clasificacionIA;
    if (!ai) {

      try {
        ai = process.env.AI_ENABLED === 'true'
          ? await httpJson(process.env.GCP_AI_BASE_URL, '/api/v1/ai/prioritize', 'POST', { usuario_id: report.user_id, zona: zone, descripcion: report.description,
            ...(report.imagen_base64 ? { imagen_base64: report.imagen_base64, imagen_mime_type: report.imagen_mime_type } : {}) }, true)
          : { prioridad: 'MEDIA', categoria: 'GENERAL', es_infraestructura_critica: false, analysis_method: 'RULES_AI_DISABLED' };
      } catch (error) {
        if ([401, 403].includes(error.statusCode) || /Secret|Configuration|InvalidGCP/.test(error.message)) throw error;
        ai = { prioridad: 'MEDIA', categoria: 'GENERAL', es_infraestructura_critica: false, analysis_method: 'RULES_AI_UNAVAILABLE' };
      }
      if (!['ALTA', 'MEDIA', 'BAJA'].includes(ai.prioridad) || typeof ai.es_infraestructura_critica !== 'boolean') throw new Error('InvalidAIContract');
      const at = now();
      const notification = await outbox('incident.status.changed', { incidenteId: data.report_id, zona: zone, estado: 'EN_REVISION' }, envelope.event_id);
      await ddb.send(new TransactWriteCommand({ TransactItems: [
        { Update: { TableName: process.env.REPORTES_TABLE, Key: { reporteId: data.report_id }, UpdateExpression: 'SET clasificacionIA = :ai, estado = :s, updatedAt = :at, prioridad = :p, categoriaIA = :c', ConditionExpression: 'attribute_not_exists(clasificacionIA)', ExpressionAttributeValues: { ':ai': ai, ':s': 'EN_REVISION', ':at': at, ':p': ai.prioridad, ':c': ai.categoria || 'GENERAL' } } },
        { Put: { TableName: process.env.HISTORIAL_TABLE, Item: audit(data.report_id, at, envelope.event_id, 'REGISTRADA', 'EN_REVISION') } },
        { Put: { TableName: process.env.OUTBOX_TABLE, Item: notification } },
      ] }));
    }

    const state = await get(process.env.STATE_TABLE, { state_key: 'ZONE#' + zone });

    if (state?.active_incident) {
      const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: state.active_incident });
      if (!incident) throw new Error('IncidentStillStarting');
      if (!(incident.reporteIds || []).includes(data.report_id)) {
        if ((incident.reporteIds || []).length >= 1000) throw new Error('IncidentReportLimit1000');
        const updated = { ...incident, reporteIds: [...(incident.reporteIds || []), data.report_id], recipient_ids: [...new Set([...(incident.recipient_ids || []), report.user_id])], revision: incident.revision + 1, updatedAt: now() };
        const notification = await outbox('incident.collective.detected', updated, envelope.event_id, { recipient_ids: [report.user_id] });
        notification.report_ids = [data.report_id];
        await ddb.send(new TransactWriteCommand({ TransactItems: [
          { Put: { TableName: process.env.INCIDENTES_TABLE, Item: updated, ConditionExpression: 'revision = :r', ExpressionAttributeValues: { ':r': incident.revision } } },
          { Put: { TableName: process.env.OUTBOX_TABLE, Item: notification } },
          { Put: { TableName: process.env.HISTORIAL_TABLE, Item: audit(incident.incidenteId, updated.updatedAt, envelope.event_id, incident.estado, incident.estado, report.user_id, 'Reporte vinculado al incidente') } },
        ] }));
      }
      return;
    }
    const threshold = Math.max(1, Number(process.env.COLLECTIVE_REPORT_THRESHOLD || 10));
    const reporterCount = await distinctReportersInZone(zone, report.problem_type || 'GENERAL', threshold);
    const antennas = await zoneAntennas(zone);
    if (reporterCount >= threshold
      || ai.es_infraestructura_critica === true || antennas.some(antenna => ['DOWN', 'DISABLED', 'SATURATED'].includes(antenna.status))) {
      await startWorkflow(zone, envelope.event_id, antennas, 0, data.report_id, report.problem_type || 'GENERAL');
    }
    return;
  }
  const recurrence = envelope.source === 'network-telemetry' ? await recordAntennaFailure(data.antenna_id, envelope) : null;
  const antennas = await zoneAntennas(zone);
  const state = await get(process.env.STATE_TABLE, { state_key: 'ZONE#' + zone });
  if (state?.active_incident) {
    const incident = await get(process.env.INCIDENTES_TABLE, { incidenteId: state.active_incident });
    if (incident) await maybeResolve(incident, envelope.event_id);
  } else if (recurrence && antennas.some(antenna => antenna.antenna_id === recurrence.antenna_id && BAD_ANTENNA_STATES.has(antenna.status))) {
    await startWorkflow(zone, envelope.event_id, antennas, 0, undefined, 'PREVENTIVE_MAINTENANCE', recurrence);
  } else if (antennas.some(antenna => BAD_ANTENNA_STATES.has(antenna.status))) await startWorkflow(zone, envelope.event_id, antennas);
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
      try { await projectMetric(envelope); }
      catch (error) { console.error('metric_projection_failed', error.name); }
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
