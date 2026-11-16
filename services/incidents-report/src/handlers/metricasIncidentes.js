const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, QueryCommand } = require('@aws-sdk/lib-dynamodb');

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const MAX_ZONES = 10;
const MAX_AGGREGATES = 50000;
const BAD_ANTENNA_STATES = new Set(['DOWN', 'DISABLED', 'SATURATED']);
const STAFF_GROUPS = new Set(['admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo']);
const headers = { 'Content-Type': 'application/json', 'Access-Control-Allow-Origin': process.env.FRONTEND_ORIGIN || '' };

function claimsFrom(event) {
  return event.requestContext?.authorizer?.claims || event.requestContext?.authorizer?.jwt?.claims || {};
}

function isStaff(event) {
  let groups = claimsFrom(event)['cognito:groups'] || [];
  if (typeof groups === 'string') {
    try { groups = JSON.parse(groups); } catch (_) { groups = groups.replace(/^\[|\]$/g, '').split(/[\s,]+/); }
  }
  return Array.isArray(groups) && groups.some(group => STAFF_GROUPS.has(String(group).trim()));
}

function identifier(value) {
  if (typeof value !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$/.test(value)) throw new Error('InvalidZone');
  return value;
}

function parseZones(query) {
  const raw = query.zonas || query.zona;
  if (raw === undefined || raw === '') return null;
  if (typeof raw !== 'string') throw new Error('InvalidZones');
  const zones = [...new Set(raw.split(',').map(value => identifier(value.trim())))];
  if (!zones.length || zones.length > MAX_ZONES) throw new Error('InvalidZones');
  return new Set(zones);
}

function parseWindow(query) {
  const toDate = query.to ? new Date(query.to) : new Date();
  const fromDate = query.from ? new Date(query.from) : new Date(toDate.getTime() - 86400000);
  if (!Number.isFinite(toDate.getTime()) || !Number.isFinite(fromDate.getTime()) || fromDate > toDate
    || toDate - fromDate > 31 * 86400000) throw new Error('InvalidInterval');
  return { fromDate, toDate, from: fromDate.toISOString(), to: toDate.toISOString() };
}

function monthsBetween(fromDate, toDate) {
  const months = [];
  const cursor = new Date(Date.UTC(fromDate.getUTCFullYear(), fromDate.getUTCMonth(), 1));
  const last = new Date(Date.UTC(toDate.getUTCFullYear(), toDate.getUTCMonth(), 1));
  while (cursor <= last) {
    months.push(cursor.toISOString().slice(0, 7));
    cursor.setUTCMonth(cursor.getUTCMonth() + 1);
  }
  return months;
}

async function readAggregates(period, from, to, remaining) {
  const items = [];
  let key;
  do {
    const page = await ddb.send(new QueryCommand({
      TableName: process.env.METRICS_TABLE,
      KeyConditionExpression: '#period = :period AND #metric BETWEEN :from AND :to',
      ExpressionAttributeNames: { '#period': 'period', '#metric': 'metric_key' },
      ExpressionAttributeValues: { ':period': period, ':from': `DAY#${from}#`, ':to': `DAY#${to}#\uffff` },
      ExclusiveStartKey: key,
      Limit: Math.min(1000, remaining - items.length + 1),
    }));
    items.push(...(page.Items || []));
    key = page.LastEvaluatedKey;
    if (items.length > remaining) throw new Error('InvalidAggregateVolume');
  } while (key);
  return items;
}

function increment(map, key, amount) {
  if (key) map[key] = (map[key] || 0) + amount;
}

function topEntries(map, limit = 10) {
  return Object.entries(map).sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, limit).map(([id, count]) => ({ id, count }));
}

function metricThreshold(name, fallback, max = 999) {
  const value = Number(process.env[name]);
  return Number.isInteger(value) && value >= 2 && value <= max ? value : fallback;
}

exports.handler = async event => {
  try {
    if (!claimsFrom(event).sub) return { statusCode: 401, headers, body: '{}' };
    if (!isStaff(event)) return { statusCode: 403, headers, body: '{}' };

    const query = event.queryStringParameters || {};
    const zones = parseZones(query);
    const { fromDate, toDate, from, to } = parseWindow(query);
    const recurrentWindowDays = metricThreshold('RECURRENT_ANTENNA_WINDOW_DAYS', 7, 31);
    const recurrenceFrom = new Date(toDate.getTime() - (recurrentWindowDays - 1) * 86400000).toISOString().slice(0, 10);
    const aggregateFrom = from.slice(0, 10) < recurrenceFrom ? from.slice(0, 10) : recurrenceFrom;
    const rows = [];
    for (const period of monthsBetween(new Date(`${aggregateFrom}T00:00:00.000Z`), toDate)) {
      rows.push(...await readAggregates(period, aggregateFrom, to.slice(0, 10), MAX_AGGREGATES - rows.length));
    }

    const reportCounts = {};
    const reportsByType = {};
    const incidentsByCriticality = {};
    const antennaFailures = {};
    const recurrenceFailures = {};
    const antennaDays = {};
    let totalReports = 0;
    let totalIncidents = 0;
    let resolutionTotal = 0;
    let resolutionCount = 0;
    for (const row of rows) {
      if (row.day < from.slice(0, 10) || row.day > to.slice(0, 10)) {
        if (!(row.kind === 'ANTENNA' && row.day >= recurrenceFrom && row.day <= to.slice(0, 10))) continue;
      }
      if (row.zone !== '-' && zones && !zones.has(row.zone)) continue;
      const count = Number(row.event_count || 0);
      if (row.kind === 'REPORT') {
        totalReports += count;
        increment(reportCounts, row.zone, count);
        increment(reportsByType, row.dimension, count);
      } else if (row.kind === 'INCIDENT') {
        totalIncidents += count;
        increment(incidentsByCriticality, row.dimension, count);
      } else if (row.kind === 'RESOLUTION') {
        resolutionTotal += Number(row.resolution_minutes_total || 0);
        resolutionCount += count;
      } else if (row.kind === 'ANTENNA' && BAD_ANTENNA_STATES.has(row.status)) {
        if (row.day >= from.slice(0, 10)) increment(antennaFailures, row.dimension, count);
        increment(recurrenceFailures, row.dimension, count);
        antennaDays[row.dimension] ||= new Set();
        antennaDays[row.dimension].add(row.day);
      }
    }

    const recurrentMinEvents = metricThreshold('RECURRENT_ANTENNA_MIN_EVENTS', 3);
    const recurrentMinDays = metricThreshold('RECURRENT_ANTENNA_MIN_DAYS', 2);
    const recurrentAntennas = Object.entries(recurrenceFailures)
      .filter(([antenna]) => recurrenceFailures[antenna] >= recurrentMinEvents && (antennaDays[antenna]?.size || 0) >= recurrentMinDays)
      .map(([antenna_id, events]) => ({ antenna_id, events, affected_days: antennaDays[antenna_id].size }))
      .sort((a, b) => b.events - a.events || a.antenna_id.localeCompare(b.antenna_id)).slice(0, 100);
    const topZones = topEntries(reportCounts, 100);
    const topTypes = topEntries(reportsByType, 100);
    const topAntennas = topEntries(antennaFailures, 100);

    return {
      statusCode: 200,
      headers,
      body: JSON.stringify({
        totalReportes: totalReports,
        totalIncidentesColectivos: totalIncidents,
        tiempoPromedioResolucionMinutos: resolutionCount ? Math.round(resolutionTotal / resolutionCount) : null,
        zonasConMasIncidentes: Object.fromEntries(topZones),
        rankingZonas: topZones,
        tiposInconvenientesFrecuentes: Object.fromEntries(topTypes),
        rankingTiposInconvenientes: topTypes,
        antenasConMasFallas: Object.fromEntries(topAntennas),
        rankingAntenas: topAntennas,
        antenasRecurrentes: recurrentAntennas,
        umbralRecurrencia: { eventos: recurrentMinEvents, diasAfectados: recurrentMinDays, ventanaDias: recurrentWindowDays },
        incidentesPorCriticidad: incidentsByCriticality,
        scope: { zonas: zones ? [...zones] : 'TODAS', from, to, aggregation: 'EVENT_PROJECTION', complete: false,
          completenessNote: 'La agregacion inicia al desplegar el proyector; el historico previo requiere backfill.' },
      }),
    };
  } catch (error) {
    const invalid = error.message.startsWith('Invalid');
    console.error('incident_metrics_failed', error.name);
    return {
      statusCode: invalid ? 400 : 503,
      headers,
      body: JSON.stringify({ message: invalid
        ? `El intervalo admite maximo 31 dias, ${MAX_ZONES} zonas y ${MAX_AGGREGATES} agregados.`
        : 'No se pudieron consultar las metricas.' }),
    };
  }
};
