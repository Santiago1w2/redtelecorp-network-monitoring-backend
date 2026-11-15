const crypto = require("crypto");
const { EventBridgeClient, PutEventsCommand } = require("@aws-sdk/client-eventbridge");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  PutCommand,
  UpdateCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const events = new EventBridgeClient({});

exports.handler = async (event) => {
  const {
    zona,
    usuariosUnicosAfectados,
    reporteIds,
    antenasNormalizadas,
    esRecurrente,
    diagnosticoIA,
  } = event;

  const incidenteId = `INC-${crypto.randomUUID()}`;
  const now = new Date().toISOString();

  const incidente = {
    incidenteId,
    zona,
    usuariosAfectados: usuariosUnicosAfectados,
    totalReportesVinculados: reporteIds.length,
    reporteIds,
    antenasAfectadas: antenasNormalizadas,
    esRecurrente,
    estado: "DETECTADO",
    statusHistory: [{ estadoAnterior: null, estadoNuevo: "DETECTADO", timestamp: now, actorId: "AWS-STEP-FUNCTIONS" }],
    prioridad: diagnosticoIA.riesgo_estructural || "ALTA",
    causaRaizProbable: diagnosticoIA.causa_raiz_probable,
    requiereCuadrillaCampo: diagnosticoIA.requiere_cuadrilla_campo,
    mensajeNotificacionUsuario: diagnosticoIA.mensaje_notificacion_usuario,
    createdAt: now,
    updatedAt: now,
  };

  // 1. Guardar Incidente Colectivo en DynamoDB
  await ddb.send(
    new PutCommand({
      TableName: process.env.INCIDENTES_TABLE,
      Item: incidente,
    })
  );

  // 2. Registrar en historial de auditoría
  await ddb.send(
    new PutCommand({
      TableName: process.env.HISTORIAL_TABLE,
      Item: {
        entidadId: incidenteId,
        timestamp: now,
        estadoAnterior: null,
        estadoNuevo: "DETECTADO",
        actorId: "AWS-STEP-FUNCTIONS",
        comentario: `Incidente colectivo detectado (${usuariosUnicosAfectados} usuarios). Diagnóstico IA: ${diagnosticoIA.causa_raiz_probable}`,
      },
    })
  );

  // 3. Vincular todos los reportes individuales al incidente colectivo
  for (const rId of reporteIds) {
    await ddb.send(
      new UpdateCommand({
        TableName: process.env.REPORTES_TABLE,
        Key: { reporteId: rId },
        UpdateExpression: "set incidenteColectivoId = :inc, updatedAt = :now",
        ExpressionAttributeValues: {
          ":inc": incidenteId,
          ":now": now,
        },
      })
    );
  }

  const eventType = "incident.collective.detected";
  const eventDetail = {
    version: "1.0",
    source: "incident-management",
    event_id: incidenteId,
    event_type: eventType,
    occurred_at: now,
    data: { incident_id: incidenteId, zone_name: zona, incident_status: "DETECTADO" },
  };
  const published = await events.send(new PutEventsCommand({ Entries: [{
    EventBusName: process.env.EVENT_BUS_NAME,
    Source: "incident-management",
    DetailType: eventType,
    Detail: JSON.stringify(eventDetail),
  }] }));
  if (published.FailedEntryCount) throw new Error("No se pudo publicar el evento de incidente colectivo");

  return {
    ...event,
    incidenteId,
    incidente,
  };
};
