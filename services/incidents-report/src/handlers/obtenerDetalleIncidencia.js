const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  GetCommand,
  QueryCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    const claims = event.requestContext?.authorizer?.claims || {};
    let grupos = claims['cognito:groups'] || [];
    if (typeof grupos === 'string') { try { grupos = JSON.parse(grupos); } catch (_) { grupos = grupos.replace(/^\[|\]$/g, '').split(','); } }
    const staff = Array.isArray(grupos) && grupos.some(group => ['admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo'].includes(group.trim()));
    if (!claims.sub) return { statusCode: 401, body: '{}' };
    const id = event.pathParameters?.id;
    const tabla = id.startsWith("INC-")
      ? process.env.INCIDENTES_TABLE
      : process.env.REPORTES_TABLE;
    const key = id.startsWith("INC-") ? { incidenteId: id } : { reporteId: id };

    const itemRes = await ddb.send(new GetCommand({ TableName: tabla, Key: key }));

    if (!itemRes.Item) {
      return {
        statusCode: 404,
        headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
        body: JSON.stringify({ message: "Incidencia no encontrada" }),
      };
    }

    if (!staff) {
      const item = itemRes.Item;
      const owns = item.userId === claims.sub || (item.recipient_ids || []).includes(claims.sub);
      if (!owns) return { statusCode: 404, body: JSON.stringify({ message: 'Incidencia no encontrada' }) };
      const fields = ['incidenteId', 'reporteId', 'zona', 'estado', 'createdAt', 'updatedAt', 'resolvedAt'];
      return { statusCode: 200, body: JSON.stringify({ detalle: Object.fromEntries(fields.filter(key => item[key] !== undefined).map(key => [key, item[key]])) }) };
    }
    const historialRes = await ddb.send(
      new QueryCommand({
        TableName: process.env.HISTORIAL_TABLE,
        KeyConditionExpression: "entidadId = :id",
        ExpressionAttributeValues: { ":id": id },
        ScanIndexForward: true,
      })
    );

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
      body: JSON.stringify({
        detalle: itemRes.Item,
        historial: historialRes.Items || [],
      }),
    };
  } catch (error) {
    console.error("Error obteniendo detalle:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
      body: JSON.stringify({ message: "Error interno", error: error.message }),
    };
  }
};