const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  GetCommand,
  UpdateCommand,
  PutCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

const ESTADOS_VALIDOS = [
  "REGISTRADA",
  "EN_REVISION",
  "SIENDO_ATENDIDA",
  "SOLUCIONADA",
  "CERRADA",
];

exports.handler = async (event) => {
  try {
    const claims = event.requestContext.authorizer?.claims || {};
    const grupos = claims["cognito:groups"] || "";

    const rolesPermitidos = ["admin", "supervisor_red", "operador_soporte", "tecnico_campo"];
    const tienePermiso = rolesPermitidos.some((r) => grupos.includes(r));

    if (!tienePermiso) {
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

    const now = new Date().toISOString();
    let extraUpdate = "";
    const exprValues = {
      ":est": nuevoEstado,
      ":now": now,
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
        UpdateExpression: `set estado = :est, updatedAt = :now${extraUpdate}`,
        ExpressionAttributeValues: exprValues,
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