const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, QueryCommand } = require("@aws-sdk/lib-dynamodb");
const { isStaff, response } = require("./auth");
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const decodeCursor = (value) => value ? JSON.parse(Buffer.from(value, "base64url").toString("utf8")) : undefined;
const encodeCursor = (value) => value ? Buffer.from(JSON.stringify(value)).toString("base64url") : null;

exports.handler = async (event) => {
  try {
    if (!isStaff(event)) return response(403, { message: "Se requiere un rol interno" });
    const zona = event.pathParameters?.zona;
    if (!zona) return response(400, { message: "zona requerida" });
    const limit = Math.max(1, Math.min(Number(event.queryStringParameters?.limit) || 50, 100));
    const result = await ddb.send(new QueryCommand({
      TableName: process.env.REPORTES_TABLE,
      IndexName: "ZonaCreatedAtIndex",
      KeyConditionExpression: "zona = :z",
      ExpressionAttributeValues: { ":z": zona },
      ScanIndexForward: false,
      Limit: limit,
      ExclusiveStartKey: decodeCursor(event.queryStringParameters?.cursor),
    }));
    return response(200, { reportes: result.Items || [], cursor: encodeCursor(result.LastEvaluatedKey) });
  } catch (error) {
    console.error("Error listando reportes de zona:", error);
    return response(500, { message: "Error al listar reportes" });
  }
};
