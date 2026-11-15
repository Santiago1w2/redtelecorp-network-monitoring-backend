
const { SignatureV4 } = require('@smithy/signature-v4');
const { HttpRequest } = require('@smithy/protocol-http');
const { Sha256 } = require('@aws-crypto/sha256-js');
const { defaultProvider } = require('@aws-sdk/credential-provider-node');
async function updateReport(reportId, envelope) {
  const base = process.env.REPORTS_API_URL;
  if (!base) throw new Error('ReportsUrlMissing');
  const url = new URL(base.replace(/\/$/, '') + '/v1/reports/' + encodeURIComponent(reportId) + '/incident');
  if (url.protocol !== 'https:' && !(process.env.LOCAL_TEST === 'true' && url.hostname === '127.0.0.1')) throw new Error('HTTPSRequired');
  const body = JSON.stringify({ incident_id: envelope.data.incident_id, status: envelope.data.incident_status, updated_at: envelope.occurred_at });
  const signer = new SignatureV4({ credentials: defaultProvider(), region: process.env.AWS_REGION || 'us-east-1', service: 'execute-api', sha256: Sha256 });
  const signed = await signer.sign(new HttpRequest({ protocol: url.protocol, hostname: url.hostname, port: url.port ? Number(url.port) : undefined,
    path: url.pathname, method: 'PATCH', headers: { host: url.host, 'content-type': 'application/json' }, body }));
  const response = await fetch(url, { method: 'PATCH', headers: signed.headers, body, signal: AbortSignal.timeout(12000) });
  if (!response.ok && response.status !== 404) throw new Error('ReportStatusHTTP' + response.status);
}
const { EventBridgeClient, PutEventsCommand } = require('@aws-sdk/client-eventbridge');
const { unmarshall } = require('@aws-sdk/util-dynamodb');
const events = new EventBridgeClient({});
exports.handler = async event => {
  for (const record of event.Records || []) {
    if (record.eventName !== 'INSERT') continue;
    const item = unmarshall(record.dynamodb.NewImage);
    const detail = item.legacy_detail ? item.detail : item.envelope;
    if (!item.legacy_detail) {
      for (let offset = 0; offset < (item.report_ids || []).length; offset += 10) {
        await Promise.all(item.report_ids.slice(offset, offset + 10).map(report => updateReport(report, detail)));
      }
    }
    const result = await events.send(new PutEventsCommand({ Entries: [{ EventBusName: process.env.EVENT_BUS_NAME,
      Source: item.source || detail.source, DetailType: item.event_type || detail.event_type, Detail: JSON.stringify(detail) }] }));
    if (result.FailedEntryCount) throw new Error('EventBridgePartialFailure');
  }
  return { published: true };
};
