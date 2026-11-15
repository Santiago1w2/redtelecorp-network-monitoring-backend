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

async function queryInterval(table, index, zone, from, to) {
  const items = []; let key;
  do {
    const page = await ddb.send(new QueryCommand({ TableName: table, IndexName: index,
      KeyConditionExpression: 'zona = :z AND createdAt BETWEEN :from AND :to', ExpressionAttributeValues: { ':z': zone, ':from': from, ':to': to }, ExclusiveStartKey: key }));
    items.push(...(page.Items || [])); key = page.LastEvaluatedKey;
    if (items.length > 10000) throw new Error('InvalidIntervalTooLarge');
  } while (key);
  return items;
}
exports.handler = async event => {
  try {
    if (!claimsFrom(event).sub) return { statusCode: 401, headers, body: '{}' };
    if (!isStaff(event)) return { statusCode: 403, headers, body: '{}' };
    const qs = event.queryStringParameters || {};
    const zone = identifier(qs.zona);
    const toDate = qs.to ? new Date(qs.to) : new Date();
    const fromDate = qs.from ? new Date(qs.from) : new Date(toDate.getTime() - 86400000);
    if (!Number.isFinite(toDate.getTime()) || !Number.isFinite(fromDate.getTime()) || fromDate > toDate || toDate - fromDate > 31 * 86400000) throw new Error('InvalidInterval');
    const from = fromDate.toISOString(), to = toDate.toISOString();
    const [reports, incidents] = await Promise.all([queryInterval(process.env.REPORTES_TABLE, 'ZonaCreatedAtIndex', zone, from, to), queryInterval(process.env.INCIDENTES_TABLE, 'ZonaIncidentesIndex', zone, from, to)]);
    const types = {}, failures = {};
    for (const report of reports) { const type = report.categoriaIA || report.tipoProblema || 'GENERAL'; types[type] = (types[type] || 0) + 1; }
    const resolved = incidents.filter(item => typeof item.tiempoResolucionMinutos === 'number');
    for (const incident of incidents) for (const antenna of incident.antenasAfectadas || []) if (['DOWN', 'DISABLED', 'SATURATED'].includes(antenna.status)) failures[antenna.antenna_id] = (failures[antenna.antenna_id] || 0) + 1;
    return { statusCode: 200, headers, body: JSON.stringify({ totalReportes: reports.length, totalIncidentesColectivos: incidents.length,
      tiempoPromedioResolucionMinutos: resolved.length ? Math.round(resolved.reduce((sum, item) => sum + item.tiempoResolucionMinutos, 0) / resolved.length) : null,
      zonasConMasIncidentes: { [zone]: reports.length }, tiposInconvenientesFrecuentes: types, antenasConMasFallas: failures, scope: { zona: zone, from, to, complete: true } }) };
  } catch (error) { return { statusCode: error.message.startsWith('Invalid') ? 400 : 503, headers, body: JSON.stringify({ message: 'Indicar zona y un intervalo de hasta 31 dias; reducir el intervalo si supera 10000 registros' }) }; }
};
