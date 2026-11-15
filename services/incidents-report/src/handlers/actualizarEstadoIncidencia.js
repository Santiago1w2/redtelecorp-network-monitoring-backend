const crypto = require("crypto");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  GetCommand,
  UpdateCommand,
  PutCommand,
} = require("@aws-sdk/lib-dynamodb");
const { EventBridgeClient, PutEventsCommand } = require("@aws-sdk/client-eventbridge");
const { claimsFrom, isStaff, response } = require("./auth");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const events = new EventBridgeClient({});
const TRANSITIONS = {
  REGISTRADA: new Set(["EN_REVISION", "CERRADA"]),
  EN_REVISION: new Set(["SIENDO_ATENDIDA", "SOLUCIONADA", "CERRADA"]),
  SIENDO_ATENDIDA: new Set(["SOLUCIONADA", "CERRADA"]),
  SOLUCIONADA: new Set(["CERRADA", "EN_REVISION"]),
  CERRADA: new Set(),
  DETECTADO: new Set(["EN_REVISION", "EN_ATENCION", "SOLUCIONADA", "CERRADA"]),
  EN_ATENCION: new Set(["SOLUCIONADA", "CERRADA"]),
};

const ESTADOS_VALIDOS = [
  "REGISTRADA",
  "EN_REVISION",
  "SIENDO_ATENDIDA",
  "SOLUCIONADA",
  "CERRADA",
  "DETECTADO",
  "EN_ATENCION",
];

exports.handler = async (event) => {
  try {
    const claims = claimsFrom(event);
    if (!isStaff(event)) {
      return {
        statusCode: 403,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: "No tiene permisos para actualizar estados" }),
      };
    }

    const id = event.pathParameters?.id;
    const { nuevoEstado, comentario } = JSON.parse(event.body || "{}");

    if (!ESTADOS_VALIDOS.includes(nuevoEstado)) {
      return {
        statusCode: 400,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: "Estado inválido", estadosPermitidos: ESTADOS_VALIDOS }),
      };
    }

    const esColectivo = id.startsWith("INC-");
    const tabla = esColectivo ? process.env.INCIDENTES_TABLE : process.env.REPORTES_TABLE;
    const key = esColectivo ? { incidenteId: id } : { reporteId: id };

    const actual = await ddb.send(new GetCommand({ TableName: tabla, Key: key }));
    if (!actual.Item) {
      return {
        statusCode: 404,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: "Registro no encontrado" }),
      };
    }
    if (!TRANSITIONS[actual.Item.estado]?.has(nuevoEstado)) {
      return {
        statusCode: 409,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: `Transición no permitida: ${actual.Item.estado} -> ${nuevoEstado}` }),
      };
    }

    const now = new Date().toISOString();
    let extraUpdate = "";
    const exprValues = {
      ":est": nuevoEstado,
      ":now": now,
      ":old": actual.Item.estado,
      ":history": [{ estadoAnterior: actual.Item.estado, estadoNuevo: nuevoEstado, timestamp: now, actorId: claims.email || claims.sub, comentario: comentario || `Estado actualizado a ${nuevoEstado}` }],
    };

    // Calcular tiempo de resolución si pasa a SOLUCIONADA
    if (nuevoEstado === "SOLUCIONADA" && actual.Item.createdAt) {
      const minutos = Math.round(
        (new Date(now) - new Date(actual.Item.createdAt)) / 60000
      );
      extraUpdate = ", tiempoResolucionMinutos = :min, resolvedAt = :now";
      exprValues[":min"] = minutos;
    }

    await ddb.send(
      new UpdateCommand({
        TableName: tabla,
        Key: key,
      UpdateExpression: `set estado = :est, updatedAt = :now, statusHistory = list_append(if_not_exists(statusHistory, :empty), :history)${extraUpdate}`,
      ConditionExpression: "estado = :old",
      ExpressionAttributeValues: { ...exprValues, ":empty": [] },
      })
    );

    await ddb.send(
      new PutCommand({
        TableName: process.env.HISTORIAL_TABLE,
        Item: {
          entidadId: id,
          timestamp: now,
          estadoAnterior: actual.Item.estado,
          estadoNuevo: nuevoEstado,
          actorId: claims.email || claims.sub,
          comentario: comentario || `Estado actualizado a ${nuevoEstado}`,
        },
      })
    );

    const zoneName = actual.Item.zona || "zona afectada";
    const eventType = ["SOLUCIONADA", "CERRADA"].includes(nuevoEstado)
      ? "incident.resolved"
      : "incident.status.changed";
    const eventDetail = {
      version: "1.0",
      source: "incident-management",
      event_id: crypto.randomUUID(),
      event_type: eventType,
      occurred_at: now,
      data: { incident_id: id, zone_name: zoneName, incident_status: nuevoEstado },
    };
    const published = await events.send(new PutEventsCommand({ Entries: [{
      EventBusName: process.env.EVENT_BUS_NAME,
      Source: "incident-management",
      DetailType: eventType,
      Detail: JSON.stringify(eventDetail),
    }] }));
    if (published.FailedEntryCount) throw new Error("No se pudo publicar el cambio de estado");

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        message: `Estado actualizado a ${nuevoEstado}`,
        id,
        estado: nuevoEstado,
      }),
    };
  } catch (error) {
    console.error("Error actualizando estado:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error interno", error: error.message }),
    };
  }
};
