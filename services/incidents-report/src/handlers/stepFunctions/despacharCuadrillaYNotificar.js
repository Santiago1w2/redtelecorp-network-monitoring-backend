const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, UpdateCommand, PutCommand } = require("@aws-sdk/lib-dynamodb");
const { EventBridgeClient, PutEventsCommand } = require("@aws-sdk/client-eventbridge");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const events = new EventBridgeClient({});

exports.handler = async (event) => {
  const { incidenteId, zona, diagnosticoIA } = event;
  const now = new Date().toISOString();
  const antennaId = (event.antenasNormalizadas || []).find((antenna) => antenna.antena_id)?.antena_id;
  const priority = ({ CRITICA: "CRITICAL", ALTA: "HIGH", MEDIA: "MEDIUM", BAJA: "LOW" })[diagnosticoIA.prioridad] || "HIGH";

  await ddb.send(new UpdateCommand({
    TableName: process.env.INCIDENTES_TABLE,
    Key: { incidenteId },
    UpdateExpression: "SET cuadrillaSolicitada = :yes, updatedAt = :now",
    ExpressionAttributeValues: { ":yes": true, ":now": now },
    ConditionExpression: "attribute_exists(incidenteId)",
  }));
  await ddb.send(new PutCommand({
    TableName: process.env.HISTORIAL_TABLE,
    Item: {
      entidadId: incidenteId,
      timestamp: now,
      estadoAnterior: "DETECTADO",
      estadoNuevo: "DETECTADO",
      actorId: "AWS-STEP-FUNCTIONS",
      comentario: `Solicitud de cuadrilla emitida para ${zona}; la asignación se confirmará con Field Operations.`,
    },
  }));

  const result = await events.send(new PutEventsCommand({ Entries: [{
    EventBusName: "default",
    Source: "redtelecorp.incident-management",
    DetailType: "IncidentNeedsTechnician",
    Detail: JSON.stringify({
      incidentId: incidenteId,
      zoneId: zona,
      requiredSkill: "NETWORK",
      description: diagnosticoIA.causa_raiz_probable || `Verificar afectación de red en ${zona}`,
      priority,
      ...(antennaId ? { antennaId } : {}),
    }),
  }] }));
  if (result.FailedEntryCount) throw new Error("No se pudo solicitar el despacho de cuadrilla");
  return { status: "SOLICITUD_CUADRILLA_ENVIADA", incidenteId, zona };
};
