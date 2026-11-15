const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, GetCommand, QueryCommand } = require("@aws-sdk/lib-dynamodb");
const { response } = require("./auth");
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const decode = (value) => value ? JSON.parse(Buffer.from(value, "base64url").toString("utf8")) : undefined;
const encode = (value) => value ? Buffer.from(JSON.stringify(value)).toString("base64url") : null;

exports.handler = async (event) => {
  try {
    const id = event.pathParameters?.id;
    if (!id || (!id.startsWith("INC-") && !id.startsWith("REP-"))) return response(400, { message: "Identificador de incidente inválido" });
    if (id.startsWith("REP-")) {
      const report = await ddb.send(new GetCommand({ TableName: process.env.REPORTES_TABLE, Key: { reporteId: id }, ProjectionExpression: "userId" }));
      if (!report.Item?.userId) return response(404, { message: "Reporte no encontrado" });
      return response(200, { recipient_ids: [report.Item.userId], cursor: null });
    }
    const limit = Math.max(1, Math.min(Number(event.queryStringParameters?.limit) || 100, 100));
    const result = await ddb.send(new QueryCommand({
      TableName: process.env.REPORTES_TABLE,
      IndexName: "IncidentReportesIndex",
      KeyConditionExpression: "incidenteColectivoId = :id",
      ExpressionAttributeValues: { ":id": id },
      Limit: limit,
      ExclusiveStartKey: decode(event.queryStringParameters?.cursor),
      ProjectionExpression: "userId",
    }));
    const recipientIds = [...new Set((result.Items || []).map((item) => item.userId).filter(Boolean))];
    return response(200, { recipient_ids: recipientIds, cursor: encode(result.LastEvaluatedKey) });
  } catch (error) {
    console.error("Error listando destinatarios del incidente:", error);
    return response(500, { message: "Error al resolver destinatarios" });
  }
};
