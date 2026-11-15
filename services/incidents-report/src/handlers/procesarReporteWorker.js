const crypto = require("crypto");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, UpdateCommand, QueryCommand, PutCommand } = require("@aws-sdk/lib-dynamodb");
const { SFNClient, StartExecutionCommand } = require("@aws-sdk/client-sfn");
const { EventBridgeClient, PutEventsCommand } = require("@aws-sdk/client-eventbridge");
const { getAiApiKey } = require("./getAiApiKey");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const sfn = new SFNClient({});
const events = new EventBridgeClient({});

async function processReport(record) {
  const msg = JSON.parse(record.body);
  const { reporteId, userId, zona, tipoProblema = "GENERAL", descripcion, imagen_mime_type, imagen_base64, antenas_estado } = msg;
  if (!reporteId || !userId || !zona || !descripcion) throw new Error("Mensaje de reporte incompleto");

  const aiPayload = { usuario_id: userId, zona, descripcion };
  if (imagen_mime_type && imagen_base64) {
    aiPayload.imagen_mime_type = imagen_mime_type;
    aiPayload.imagen_base64 = imagen_base64;
  }
  let aiResult = {
    categoria: "General", prioridad: "MEDIA", es_infraestructura_critica: false,
    evidencia_visual_detectada: null, resumen_tecnico: descripcion,
  };
  try {
    const response = await fetch(`${process.env.GCP_AI_BASE_URL}/api/v1/ai/prioritize`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "x-api-key": await getAiApiKey() },
      body: JSON.stringify(aiPayload),
      signal: AbortSignal.timeout(8000),
    });
    if (response.ok) aiResult = await response.json();
    else console.error("GCP prioritize respondió HTTP", response.status);
  } catch (err) {
    console.error("Fallo llamando a GCP Prioritize IA:", err.message);
  }

  const now = new Date().toISOString();
  let statusChanged = false;
  try {
    await ddb.send(new UpdateCommand({
      TableName: process.env.REPORTES_TABLE,
      Key: { reporteId },
      UpdateExpression: "SET estado = :est, prioridad = :prio, categoriaIA = :cat, resumenTecnicoIA = :res, esInfraestructuraCritica = :crit, evidenciaVisual = :ev, updatedAt = :now, statusHistory = list_append(if_not_exists(statusHistory, :empty), :history)",
      ConditionExpression: "estado = :registered",
      ExpressionAttributeValues: {
      ":est": "EN_REVISION", ":prio": aiResult.prioridad || "MEDIA", ":cat": aiResult.categoria || "General",
      ":res": aiResult.resumen_tecnico || descripcion, ":crit": Boolean(aiResult.es_infraestructura_critica),
      ":ev": aiResult.evidencia_visual_detectada || null, ":now": now, ":empty": [],
      ":history": [{ estadoAnterior: "REGISTRADA", estadoNuevo: "EN_REVISION", timestamp: now, actorId: "GCP-AI-PRIORITIZE" }],
      ":registered": "REGISTRADA",
      },
    }));
    statusChanged = true;
  } catch (error) {
    if (error.name !== "ConditionalCheckFailedException") throw error;
  }
  if (statusChanged) {
    await ddb.send(new PutCommand({
      TableName: process.env.HISTORIAL_TABLE,
      Item: { entidadId: reporteId, timestamp: now, estadoAnterior: "REGISTRADA", estadoNuevo: "EN_REVISION", actorId: "GCP-AI-PRIORITIZE", comentario: `Clasificado por IA: ${aiResult.categoria} | Prioridad: ${aiResult.prioridad}` },
    }));
  }
  const statusEventType = "incident.status.changed";
  const statusEvent = {
    version: "1.0", source: "incident-management", event_id: `report-${reporteId}-review`,
    event_type: statusEventType, occurred_at: now,
    data: { incident_id: reporteId, zone_name: zona, incident_status: "EN_REVISION" },
  };
  const published = await events.send(new PutEventsCommand({ Entries: [{
    EventBusName: process.env.EVENT_BUS_NAME,
    Source: "incident-management",
    DetailType: statusEventType,
    Detail: JSON.stringify(statusEvent),
  }] }));
  if (published.FailedEntryCount) throw new Error("No se pudo publicar la actualización del reporte");

  const since = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
  const uniqueUsers = new Set();
  let lastKey;
  do {
    const page = await ddb.send(new QueryCommand({
      TableName: process.env.REPORTES_TABLE,
      IndexName: "ZonaCreatedAtIndex",
      KeyConditionExpression: "zona = :z AND createdAt >= :desde",
      FilterExpression: "tipoProblema = :tipo",
      ExpressionAttributeValues: { ":z": zona, ":desde": since, ":tipo": tipoProblema },
      ProjectionExpression: "userId",
      ExclusiveStartKey: lastKey,
    }));
    for (const item of page.Items || []) if (item.userId) uniqueUsers.add(item.userId);
    lastKey = page.LastEvaluatedKey;
  } while (lastKey && uniqueUsers.size < 10);

  // El incidente colectivo se crea solo al alcanzar el umbral definido por el reto.
  if (uniqueUsers.size >= 10) {
    const zoneHash = crypto.createHash("sha256").update(zona).digest("hex").slice(0, 20);
    const windowId = Math.floor(Date.now() / (24 * 60 * 60 * 1000));
    const executionName = `zone-${zoneHash}-${windowId}`;
    try {
      await sfn.send(new StartExecutionCommand({
        stateMachineArn: process.env.STATE_MACHINE_ARN,
        name: executionName,
        input: JSON.stringify({ zona, tipoProblema, windowId, triggerReporteId: reporteId, usuariosUnicosAfectados: uniqueUsers.size, antenas_estado: antenas_estado || [] }),
      }));
    } catch (err) {
      if (err.name !== "ExecutionAlreadyExists") throw err;
    }
  }
}

exports.handler = async (event) => {
  const failures = [];
  for (const record of event.Records || []) {
    try {
      await processReport(record);
    } catch (error) {
      console.error("Error procesando reporte", record.messageId, error.message);
      failures.push({ itemIdentifier: record.messageId });
    }
  }
  return { batchItemFailures: failures };
};
