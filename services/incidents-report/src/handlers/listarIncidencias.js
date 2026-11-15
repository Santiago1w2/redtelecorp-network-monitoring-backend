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
    let grupos = claims['cognito:groups'] || [];
    if (typeof grupos === 'string') { try { grupos = JSON.parse(grupos); } catch (_) { grupos = grupos.replace(/^\[|\]$/g, '').split(','); } }
    const staff = Array.isArray(grupos) && grupos.some(group => ['admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo'].includes(group.trim()));
    const userId = claims.sub;
    const qs = event.queryStringParameters || {};
    if (!userId) return { statusCode: 401, body: '{}' };
    if (qs.tipo === 'colectivos') {
      if (!staff) return { statusCode: 403, body: '{}' };
      if (!qs.zona) return { statusCode: 400, body: JSON.stringify({ message: 'zona requerida' }) };
      const limit = Number(qs.limit || 50);
      if (!Number.isInteger(limit) || limit < 1 || limit > 100) return { statusCode: 400, body: '{}' };
      let key;
      if (qs.cursor) {
        key = JSON.parse(Buffer.from(qs.cursor, 'base64url').toString());
        if (key.zona !== qs.zona) return { statusCode: 400, body: '{}' };
      }
      const page = await ddb.send(new QueryCommand({ TableName: process.env.INCIDENTES_TABLE, IndexName: 'ZonaIncidentesIndex',
        KeyConditionExpression: 'zona = :z', ExpressionAttributeValues: { ':z': qs.zona }, Limit: limit,
        ScanIndexForward: false, ExclusiveStartKey: key }));
      return { statusCode: 200, body: JSON.stringify({ incidentes: page.Items || [],
        cursor: page.LastEvaluatedKey ? Buffer.from(JSON.stringify(page.LastEvaluatedKey)).toString('base64url') : null }) };
    }

    // Si es cliente, solo puede ver sus propios reportes
    if (!staff) {
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
        headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
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
        headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
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
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
      body: JSON.stringify({ reportes: res.Items || [] }),
    };
  } catch (error) {
    console.error("Error listando incidencias:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
      body: JSON.stringify({ message: "Error al listar incidencias", error: error.message }),
    };
  }
};
