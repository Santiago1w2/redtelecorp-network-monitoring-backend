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
    const caller = claimsFrom(event).sub;
    if (!caller) return { statusCode: 401, headers, body: '{}' };
    const id = identifier(event.pathParameters?.id);
    const collective = id.startsWith('INC-');
    const item = await get(collective ? process.env.INCIDENTES_TABLE : process.env.REPORTES_TABLE, collective ? { incidenteId: id } : { reporteId: id });
    if (!item || (!isStaff(event) && item.userId !== caller && !(item.recipient_ids || []).includes(caller))) return { statusCode: 404, headers, body: '{}' };
    if (!isStaff(event) && collective) {
      const allowed = ['incidenteId', 'zona', 'estado', 'createdAt', 'updatedAt', 'resolvedAt'];
      return { statusCode: 200, headers, body: JSON.stringify({ detalle: Object.fromEntries(allowed.filter(field => item[field] !== undefined).map(field => [field, item[field]])) }) };
    }
    const { limit, key } = pagination(event, 'entidadId', id);
    const page = await ddb.send(new QueryCommand({ TableName: process.env.HISTORIAL_TABLE, KeyConditionExpression: 'entidadId = :id', ExpressionAttributeValues: { ':id': id }, ScanIndexForward: false, Limit: limit, ExclusiveStartKey: key, ConsistentRead: true }));
    const { payload_hash, imagen_base64, ...detail } = item;
    return { statusCode: 200, headers, body: JSON.stringify({ detalle: detail, historial: page.Items || [], cursor: page.LastEvaluatedKey ? Buffer.from(JSON.stringify(page.LastEvaluatedKey)).toString('base64url') : null }) };
  } catch (error) { return { statusCode: error.message.startsWith('Invalid') ? 400 : 503, headers, body: JSON.stringify({ message: 'No se pudo consultar el detalle' }) }; }
};
