const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  QueryCommand,
  ScanCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    const claims = event.requestContext.authorizer?.claims || {};
    const grupos = claims["cognito:groups"] || "";
    const userId = claims.sub;
    const qs = event.queryStringParameters || {};

    // Si es cliente, solo puede ver sus propios reportes
    if (grupos === "clientes" || (Array.isArray(grupos) && grupos.includes("clientes") && grupos.length === 1)) {
      const res = await ddb.send(
        new QueryCommand({
          TableName: process.env.REPORTES_TABLE,
          IndexName: "UserCreatedAtIndex",
          KeyConditionExpression: "userId = :u",
          ExpressionAttributeValues: { ":u": userId },
          ScanIndexForward: false,
        })
      );
      return {
        statusCode: 200,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ reportes: res.Items || [] }),
      };
    }

    // Personal interno: puede filtrar por zona o listar todos
    if (qs.zona) {
      const res = await ddb.send(
        new QueryCommand({
          TableName: process.env.REPORTES_TABLE,
          IndexName: "ZonaCreatedAtIndex",
          KeyConditionExpression: "zona = :z",
          ExpressionAttributeValues: { ":z": qs.zona },
          ScanIndexForward: false,
        })
      );
      return {
        statusCode: 200,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ reportes: res.Items || [] }),
      };
    }

    const res = await ddb.send(
      new ScanCommand({
        TableName: process.env.REPORTES_TABLE,
        Limit: 100,
      })
    );

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ reportes: res.Items || [] }),
    };
  } catch (error) {
    console.error("Error listando incidencias:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error al listar incidencias", error: error.message }),
    };
  }
};