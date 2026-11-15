const crypto = require('crypto');
const { SQSClient, SendMessageCommand } = require('@aws-sdk/client-sqs');
const sqs = new SQSClient({});
exports.handler = async event => {
  const data = event.detail || {};
  const type = { WorkOrderAssigned: 'work_order.assigned', WorkOrderPending: 'work_order.pending', WorkOrderStatusChanged: 'work_order.status.changed' }[event['detail-type']];
  if (event.source !== 'redtelecorp.field-operations' || !type || !data.incidentId || !data.workOrderId || !['PENDING_ASSIGNMENT', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'].includes(data.status)) throw new Error('InvalidWorkOrderEvent');
  const envelope = { version: '1.0', event_id: 'EVT-' + crypto.createHash('sha256').update(event.id || JSON.stringify(data)).digest('hex'), event_type: type, source: 'field-operations', occurred_at: event.time || data.updatedAt, data: { incident_id: data.incidentId, work_order_id: data.workOrderId, status: data.status } };
  await sqs.send(new SendMessageCommand({ QueueUrl: process.env.INCIDENT_INPUT_QUEUE_URL, MessageBody: JSON.stringify(envelope) }));
  return { queued: true };
};
