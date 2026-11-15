const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  GetCommand,
  QueryCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    const id = event.pathParameters?.id;
    const tabla = id.startsWith("INC-")
      ? process.env.INCIDENTES_TABLE
      : process.env.REPORTES_TABLE;
    const key = id.startsWith("INC-") ? { incidenteId: id } : { reporteId: id };

    const itemRes = await ddb.send(new GetCommand({ TableName: tabla, Key: key }));

    if (!itemRes.Item) {
      return {
        statusCode: 404,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: "Incidencia no encontrada" }),
      };
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
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        detalle: itemRes.Item,
        historial: historialRes.Items || [],
      }),
    };
  } catch (error) {
    console.error("Error obteniendo detalle:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error interno", error: error.message }),
    };
  }
};