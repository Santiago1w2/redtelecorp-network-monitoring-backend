const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand, PutCommand, TransactWriteCommand } = require('@aws-sdk/lib-dynamodb');
const { EventBridgeClient, PutEventsCommand } = require('@aws-sdk/client-eventbridge');
const { SQSClient, SendMessageCommand } = require('@aws-sdk/client-sqs');
const { unmarshall } = require('@aws-sdk/util-dynamodb');
const { SignatureV4 } = require('@smithy/signature-v4');
const { HttpRequest } = require('@smithy/protocol-http');
const { Sha256 } = require('@aws-crypto/sha256-js');
const { defaultProvider } = require('@aws-sdk/credential-provider-node');
const { projectMetric } = require('../lib/projectMetric');
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}), { marshallOptions: { removeUndefinedValues: true } });
const events = new EventBridgeClient({});
const sqs = new SQSClient({});

async function projectReport(reportId, envelope) {
  const report = (await ddb.send(new GetCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId: reportId }, ConsistentRead: true }))).Item;
  if (!report) throw new Error('ProjectionReportMissing');
  const data = envelope.data;
  const at = envelope.occurred_at;
  const status = { DETECTADO: 'EN_REVISION', EN_ATENCION: 'SIENDO_ATENDIDA' }[data.incident_status] || data.incident_status;
  const linked = data.incident_id.startsWith('INC-');
  if (!report.incident_updated_at || report.incident_updated_at < at) {
    try {
      await ddb.send(new TransactWriteCommand({ TransactItems: [
        { Update: { TableName: process.env.REPORTES_TABLE, Key: { reporteId: reportId },
          UpdateExpression: 'SET estado = :s, updatedAt = :at, incident_updated_at = :at' + (linked ? ', incidenteColectivoId = :i' : ''),
          ConditionExpression: 'attribute_exists(reporteId) AND (attribute_not_exists(incident_updated_at) OR incident_updated_at < :at)',
          ExpressionAttributeValues: { ':s': status, ':at': at, ...(linked ? { ':i': data.incident_id } : {}) } } },
        { Put: { TableName: process.env.HISTORIAL_TABLE, Item: { entidadId: reportId, timestamp: at + '#' + envelope.event_id,
          occurred_at: at, estadoAnterior: report.estado, estadoNuevo: status, actorId: 'INCIDENT-MANAGEMENT', comentario: linked ? 'Estado del incidente ' + data.incident_id : 'Estado del reporte' } } },
      ] }));
    } catch (error) {
      if (error.name !== 'TransactionCanceledException') throw error;
      const latest = (await ddb.send(new GetCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId: reportId }, ConsistentRead: true }))).Item;
      if (!latest || !latest.incident_updated_at || latest.incident_updated_at < at) throw error;
    }
  }
  if (linked) await ddb.send(new PutCommand({ TableName: process.env.RECIPIENTS_TABLE, Item: { incident_id: data.incident_id, recipient_id: report.userId } }));
  if (linked && report.legacy_source && process.env.LEGACY_REPORTS_API_URL) {
    const url = new URL(process.env.LEGACY_REPORTS_API_URL.replace(/\/$/, '') + '/v1/reports/' + encodeURIComponent(reportId) + '/incident');
    if (url.protocol !== 'https:' && !(process.env.LOCAL_TEST === 'true' && url.hostname === '127.0.0.1')) throw new Error('HTTPSRequired');
    const body = JSON.stringify({ incident_id: data.incident_id, status: data.incident_status, updated_at: at });
    const signer = new SignatureV4({ credentials: defaultProvider(), region: process.env.AWS_REGION || 'us-east-1', service: 'execute-api', sha256: Sha256 });
    const signed = await signer.sign(new HttpRequest({ protocol: url.protocol, hostname: url.hostname, port: url.port ? Number(url.port) : undefined, path: url.pathname, method: 'PATCH', headers: { host: url.host, 'content-type': 'application/json' }, body }));
    const response = await fetch(url, { method: 'PATCH', headers: signed.headers, body, signal: AbortSignal.timeout(12000) });
    if (!response.ok) throw new Error('LegacyReportHTTP' + response.status);
  }
}
exports.handler = async event => {
  for (const record of event.Records || []) {
    if (record.eventName !== 'INSERT') continue;
    const item = unmarshall(record.dynamodb.NewImage);
    const detail = item.legacy_detail ? item.detail : item.envelope;
    if (!item.legacy_detail) {
      try { await projectMetric(detail); }
      catch (error) { console.error('metric_projection_failed', error.name); }
    }
    if (item.destination === 'INCIDENT_INPUT') {
      await sqs.send(new SendMessageCommand({ QueueUrl: process.env.INCIDENT_INPUT_QUEUE_URL, MessageBody: JSON.stringify(item.envelope) }));
      continue;
    }
    if (!item.legacy_detail) {
      for (let offset = 0; offset < (item.report_ids || []).length; offset += 10) await Promise.all(item.report_ids.slice(offset, offset + 10).map(report => projectReport(report, detail)));
    }
    const result = await events.send(new PutEventsCommand({ Entries: [{ EventBusName: process.env.EVENT_BUS_NAME,
      Source: item.source || detail.source, DetailType: item.event_type || detail.event_type, Detail: JSON.stringify(detail) }] }));
    if (result.FailedEntryCount) throw new Error('EventBridgePartialFailure');
  }
  return { published: true };
};
