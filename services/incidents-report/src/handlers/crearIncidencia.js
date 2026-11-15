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

function claimsFrom(event) { return event.requestContext?.authorizer?.claims || event.requestContext?.authorizer?.jwt?.claims || {}; }
function isStaff(event) {
  let groups = claimsFrom(event)['cognito:groups'] || [];
  if (typeof groups === 'string') { try { groups = JSON.parse(groups); } catch (_) { groups = groups.replace(/^\[|\]$/g, '').split(/[\s,]+/); } }
  return Array.isArray(groups) && groups.some(group => ['admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo'].includes(String(group).trim()));
}
const headers = { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': process.env.FRONTEND_ORIGIN || '' };
function pagination(event, field, expected) {
  const query = event.queryStringParameters || {};
  const limit = Number(query.limit || 50);
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) throw new Error('InvalidLimit');
  let key;
  if (query.cursor) {
    try { key = JSON.parse(Buffer.from(query.cursor, 'base64url').toString()); } catch (_) { throw new Error('InvalidCursor'); }
    if (!key || key[field] !== expected) throw new Error('InvalidCursor');
  }
  return { limit, key };
}

exports.handler = async event => {
  try {
    const userId = claimsFrom(event).sub;
    if (!userId) return { statusCode: 401, headers, body: JSON.stringify({ message: 'Autenticacion requerida' }) };
    const body = JSON.parse(event.body || '{}');
    const zone = identifier(body.zone_id || body.zona);
    const description = body.description || body.descripcion;
    const type = body.problem_type || body.tipoProblema || 'GENERAL';
    if (typeof description !== 'string' || !description.trim() || description.length > 1500 || typeof type !== 'string' || !/^[A-Z][A-Z0-9_]{0,63}$/.test(type)) throw new Error('InvalidReport');
    if (body.numeroServicio !== undefined && (typeof body.numeroServicio !== 'string' || body.numeroServicio.length > 64)) throw new Error('InvalidService');
    if (body.imagen_base64 !== undefined && (typeof body.imagen_base64 !== 'string' || body.imagen_base64.length > 60000 || !/^[A-Za-z0-9+/]*={0,2}$/.test(body.imagen_base64) || !['image/jpeg', 'image/png', 'image/webp'].includes(body.imagen_mime_type))) throw new Error('InvalidImage');
    const idempotency = Object.entries(event.headers || {}).find(([key]) => key.toLowerCase() === 'idempotency-key')?.[1];
    if (idempotency && (typeof idempotency !== 'string' || idempotency.length > 128)) throw new Error('InvalidIdempotencyKey');
    const reporteId = 'REP-' + (idempotency ? hash(userId + ':' + idempotency).slice(0, 32) : crypto.randomUUID());
    const at = now();
    const payloadHash = hash(JSON.stringify({ zone, description, type, service: body.numeroServicio || '', image: body.imagen_base64 || '' }));
    const item = { reporteId, userId, zona: zone, descripcion: description.trim(), tipoProblema: type, createdAt: at, updatedAt: at, estado: 'REGISTRADA', payload_hash: payloadHash,
      ...(body.numeroServicio ? { numeroServicio: body.numeroServicio } : {}), ...(body.imagen_base64 ? { imagen_base64: body.imagen_base64, imagen_mime_type: body.imagen_mime_type } : {}) };
    const eventId = 'EVT-' + hash(reporteId + ':created');
    const envelope = { event_id: eventId, event_type: 'report.created', source: 'incident-management', occurred_at: at, version: '1.0', data: { report_id: reporteId, user_id: userId, zone_id: zone, problem_type: type } };
    try { await ddb.send(new TransactWriteCommand({ TransactItems: [
      { Put: { TableName: process.env.REPORTES_TABLE, Item: item, ConditionExpression: 'attribute_not_exists(reporteId)' } },
      { Put: { TableName: process.env.HISTORIAL_TABLE, Item: audit(reporteId, at, eventId, null, 'REGISTRADA', userId, 'Reporte registrado') } },
      { Put: { TableName: process.env.OUTBOX_TABLE, Item: { event_id: eventId, envelope, destination: 'INCIDENT_INPUT', expires_at: Math.floor(Date.now() / 1000) + 2592000 } } },
    ] })); } catch (error) {
      const existing = await get(process.env.REPORTES_TABLE, { reporteId });
      if (!existing) throw error;
      if (existing.userId !== userId || existing.payload_hash !== payloadHash) return { statusCode: 409, headers, body: JSON.stringify({ message: 'Idempotency-Key reutilizada con otros datos' }) };
    }
    return { statusCode: 202, headers, body: JSON.stringify({ message: 'Reporte registrado para evaluacion', reporteId, reporte: { reporteId, userId, estado: 'REGISTRADA' } }) };
  } catch (error) {
    const statusCode = error.name === 'SyntaxError' || error.message.startsWith('Invalid') ? 400 : 503;
    console.error('report_create_failed', error.name);
    return { statusCode, headers, body: JSON.stringify({ message: 'No se pudo registrar el reporte' }) };
  }
};
