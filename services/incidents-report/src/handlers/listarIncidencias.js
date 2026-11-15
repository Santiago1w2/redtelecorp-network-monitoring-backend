const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, QueryCommand, ScanCommand } = require("@aws-sdk/lib-dynamodb");
const { claimsFrom, isStaff, response } = require("./auth");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const decodeCursor = (value) => value ? JSON.parse(Buffer.from(value, "base64url").toString("utf8")) : undefined;
const encodeCursor = (value) => value ? Buffer.from(JSON.stringify(value)).toString("base64url") : null;

exports.handler = async (event) => {
  try {
    const claims = claimsFrom(event);
    const userId = claims.sub;
    const qs = event.queryStringParameters || {};
    const limit = Math.max(1, Math.min(Number(qs.limit) || 50, 100));
    const exclusiveStartKey = decodeCursor(qs.cursor);
    let result;

    if (!isStaff(event)) {
      if (!userId) return response(401, { message: "Token Cognito inválido" });
      result = await ddb.send(new QueryCommand({
        TableName: process.env.REPORTES_TABLE,
        IndexName: "UserCreatedAtIndex",
        KeyConditionExpression: "userId = :u",
        ExpressionAttributeValues: { ":u": userId },
        ScanIndexForward: false,
        Limit: limit,
        ExclusiveStartKey: exclusiveStartKey,
      }));
    } else if (qs.zona) {
      result = await ddb.send(new QueryCommand({
        TableName: process.env.REPORTES_TABLE,
        IndexName: "ZonaCreatedAtIndex",
        KeyConditionExpression: "zona = :z",
        ExpressionAttributeValues: { ":z": qs.zona },
        ScanIndexForward: false,
        Limit: limit,
        ExclusiveStartKey: exclusiveStartKey,
      }));
    } else {
      result = await ddb.send(new ScanCommand({
        TableName: process.env.REPORTES_TABLE,
        Limit: limit,
        ExclusiveStartKey: exclusiveStartKey,
      }));
    }

    return response(200, { reportes: result.Items || [], cursor: encodeCursor(result.LastEvaluatedKey) });
  } catch (error) {
    console.error("Error listando incidencias:", error);
    return response(500, { message: "Error al listar incidencias" });
  }
};
