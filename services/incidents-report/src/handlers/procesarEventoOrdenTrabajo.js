const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, GetCommand, UpdateCommand, PutCommand } = require("@aws-sdk/lib-dynamodb");
const { EventBridgeClient, PutEventsCommand } = require("@aws-sdk/client-eventbridge");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const events = new EventBridgeClient({});
const workOrderRank = { PENDING_ASSIGNMENT: 1, ASSIGNED: 2, IN_PROGRESS: 3, COMPLETED: 4, CANCELLED: 4 };

exports.handler = async (event) => {
  const detail = event.detail || {};
  const incidentId = detail.incidentId;
  const workOrderId = detail.workOrderId;
  const orderStatus = detail.status;
  if (!incidentId || !workOrderId || !workOrderRank[orderStatus]) throw new Error("Contrato Work Order inválido");

  const current = await ddb.send(new GetCommand({ TableName: process.env.INCIDENTES_TABLE, Key: { incidenteId: incidentId }, ConsistentRead: true }));
  if (!current.Item) throw new Error(`Incidente ${incidentId} no encontrado`);
  const previousOrderStatus = current.Item.workOrderStatus;
  if (previousOrderStatus && workOrderRank[previousOrderStatus] > workOrderRank[orderStatus]) return { ignored: "evento atrasado" };

  let incidentStatus = current.Item.estado;
  let reportStatus = null;
  if (orderStatus === "ASSIGNED" || orderStatus === "IN_PROGRESS") {
    incidentStatus = "EN_ATENCION";
    reportStatus = "SIENDO_ATENDIDA";
  } else if (orderStatus === "COMPLETED") {
    incidentStatus = "SOLUCIONADA";
    reportStatus = "SOLUCIONADA";
  }
  const now = new Date().toISOString();
  const statusChanged = incidentStatus !== current.Item.estado;

  const updateValues = {
    ":orderStatus": orderStatus,
    ":technician": detail.assignedTechnicianId || null,
    ":now": now,
    ":incidentStatus": incidentStatus,
    ":empty": [],
    ":history": statusChanged ? [{ estadoAnterior: current.Item.estado, estadoNuevo: incidentStatus, timestamp: now, actorId: "FIELD-OPERATIONS", comentario: `Orden ${workOrderId}: ${orderStatus}` }] : [],
  };
  const updateCondition = previousOrderStatus ? "workOrderStatus = :previous" : "attribute_not_exists(workOrderStatus)";
  if (previousOrderStatus) updateValues[":previous"] = previousOrderStatus;
  await ddb.send(new UpdateCommand({
    TableName: process.env.INCIDENTES_TABLE,
    Key: { incidenteId },
    UpdateExpression: "SET workOrderStatus = :orderStatus, assignedTechnicianId = :technician, updatedAt = :now, estado = :incidentStatus, statusHistory = list_append(if_not_exists(statusHistory, :empty), :history)",
    ConditionExpression: updateCondition,
    ExpressionAttributeValues: updateValues,
  }));

  if (statusChanged || reportStatus) {
    await ddb.send(new PutCommand({
      TableName: process.env.HISTORIAL_TABLE,
      Item: { entidadId: incidentId, timestamp: now, estadoAnterior: current.Item.estado, estadoNuevo: incidentStatus, actorId: "FIELD-OPERATIONS", comentario: `Orden ${workOrderId}: ${orderStatus}` },
    }));
  }

  if (reportStatus) {
    for (const reporteId of current.Item.reporteIds || []) {
      const oldReport = await ddb.send(new GetCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId }, ProjectionExpression: "estado" }));
      if (!oldReport.Item || oldReport.Item.estado === reportStatus) continue;
      await ddb.send(new UpdateCommand({
        TableName: process.env.REPORTES_TABLE,
        Key: { reporteId },
        UpdateExpression: "SET estado = :next, updatedAt = :now, statusHistory = list_append(if_not_exists(statusHistory, :empty), :history)",
        ConditionExpression: "estado = :previous",
        ExpressionAttributeValues: {
          ":next": reportStatus,
          ":previous": oldReport.Item.estado,
          ":now": now,
          ":empty": [],
          ":history": [{ estadoAnterior: oldReport.Item.estado, estadoNuevo: reportStatus, timestamp: now, actorId: "FIELD-OPERATIONS" }],
        },
      }));
      await ddb.send(new PutCommand({
        TableName: process.env.HISTORIAL_TABLE,
        Item: { entidadId: reporteId, timestamp: now, estadoAnterior: oldReport.Item.estado, estadoNuevo: reportStatus, actorId: "FIELD-OPERATIONS", comentario: `Orden ${workOrderId}: ${orderStatus}` },
      }));
    }
  }

  if (statusChanged) {
    const eventType = incidentStatus === "SOLUCIONADA" ? "incident.resolved" : "incident.status.changed";
    const eventDetail = {
      version: "1.0", source: "incident-management", event_id: `workorder-${workOrderId}-${orderStatus}`,
      event_type: eventType, occurred_at: now,
      data: { incident_id: incidentId, zone_name: current.Item.zona, incident_status: incidentStatus },
    };
    const published = await events.send(new PutEventsCommand({ Entries: [{
      EventBusName: process.env.EVENT_BUS_NAME,
      Source: "incident-management",
      DetailType: eventType,
      Detail: JSON.stringify(eventDetail),
    }] }));
    if (published.FailedEntryCount) throw new Error("No se pudo publicar la actualización de la incidencia");
  }
  return { incidentId, orderStatus, incidentStatus };
};
