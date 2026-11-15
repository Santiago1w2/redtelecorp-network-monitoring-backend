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

const transitions = { REGISTRADA: ['EN_REVISION'], DETECTADO: ['EN_REVISION', 'EN_ATENCION', 'SOLUCIONADA'], EN_REVISION: ['SIENDO_ATENDIDA', 'EN_ATENCION', 'SOLUCIONADA'], SIENDO_ATENDIDA: ['SOLUCIONADA'], EN_ATENCION: ['SOLUCIONADA'], SOLUCIONADA: ['CERRADA'], CERRADA: [] };
exports.handler = async event => {
  try {
    if (!claimsFrom(event).sub) return { statusCode: 401, headers, body: '{}' };
    if (!isStaff(event)) return { statusCode: 403, headers, body: '{}' };
    let id = identifier(event.pathParameters?.id);
    const body = JSON.parse(event.body || '{}');
    const status = body.nuevoEstado || body.status;
    if (!Object.hasOwn(transitions, status) || (body.comentario && (typeof body.comentario !== 'string' || body.comentario.length > 500))) throw new Error('InvalidStatus');
    let item = await get(id.startsWith('INC-') ? process.env.INCIDENTES_TABLE : process.env.REPORTES_TABLE, id.startsWith('INC-') ? { incidenteId: id } : { reporteId: id });
    if (!item) return { statusCode: 404, headers, body: '{}' };
    if (item.incidenteColectivoId) { id = item.incidenteColectivoId; item = await get(process.env.INCIDENTES_TABLE, { incidenteId: id }); }
    if (!transitions[item.estado]?.includes(status)) return { statusCode: 409, headers, body: JSON.stringify({ message: 'Transicion no permitida' }) };
    const eventId = 'EVT-' + crypto.randomUUID();
    const envelope = { event_id: eventId, event_type: 'incident.status.requested', source: 'incident-management', occurred_at: now(), version: '1.0', data: { incident_id: id, status, expected_state: item.estado, actor_id: claimsFrom(event).sub, comment: body.comentario || '' } };
    await ddb.send(new PutCommand({ TableName: process.env.OUTBOX_TABLE, Item: { event_id: eventId, envelope, destination: 'INCIDENT_INPUT', expires_at: Math.floor(Date.now() / 1000) + 2592000 } }));
    return { statusCode: 202, headers, body: JSON.stringify({ message: 'Cambio solicitado; se verificara la evidencia de recuperacion y la orden', id, requested_status: status }) };
  } catch (error) { return { statusCode: error.name === 'SyntaxError' || error.message.startsWith('Invalid') ? 400 : 503, headers, body: JSON.stringify({ message: 'No se pudo solicitar el cambio' }) }; }
};
