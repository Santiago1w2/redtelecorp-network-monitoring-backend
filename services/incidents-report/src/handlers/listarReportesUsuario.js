const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, QueryCommand } = require("@aws-sdk/lib-dynamodb");
const { claimsFrom, isStaff, response } = require("./auth");
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const decodeCursor = (value) => value ? JSON.parse(Buffer.from(value, "base64url").toString("utf8")) : undefined;
const encodeCursor = (value) => value ? Buffer.from(JSON.stringify(value)).toString("base64url") : null;

exports.handler = async (event) => {
  try {
    const requestedUser = event.pathParameters?.userId;
    const caller = claimsFrom(event).sub;
    if (!requestedUser) return response(400, { message: "userId requerido" });
    if (!isStaff(event) && requestedUser !== caller) return response(403, { message: "Solo puedes consultar tus propios reportes" });
    const limit = Math.max(1, Math.min(Number(event.queryStringParameters?.limit) || 50, 100));
    const result = await ddb.send(new QueryCommand({
      TableName: process.env.REPORTES_TABLE,
      IndexName: "UserCreatedAtIndex",
      KeyConditionExpression: "userId = :u",
      ExpressionAttributeValues: { ":u": requestedUser },
      ScanIndexForward: false,
      Limit: limit,
      ExclusiveStartKey: decodeCursor(event.queryStringParameters?.cursor),
    }));
    return response(200, { reportes: result.Items || [], cursor: encodeCursor(result.LastEvaluatedKey) });
  } catch (error) {
    console.error("Error listando reportes del usuario:", error);
    return response(500, { message: "Error al listar reportes" });
  }
};
