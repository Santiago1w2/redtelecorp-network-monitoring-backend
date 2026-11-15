const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, GetCommand } = require("@aws-sdk/lib-dynamodb");
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    const userId = event.pathParameters?.userId;
    if (!userId || !/^[A-Za-z0-9-]{1,128}$/.test(userId)) {
      return { statusCode: 400, body: JSON.stringify({ message: "userId inválido" }) };
    }
    const result = await ddb.send(new GetCommand({
      TableName: process.env.USERS_TABLE,
      Key: { userId },
      ProjectionExpression: "userId, email, phone, active",
    }));
    if (!result.Item) return { statusCode: 404, body: JSON.stringify({ message: "Usuario no encontrado" }) };
    return {
      statusCode: 200,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        user_id: result.Item.userId,
        email: result.Item.email || null,
        phone_e164: result.Item.phone || null,
        active: result.Item.active !== false,
      }),
    };
  } catch (error) {
    console.error("Error obteniendo contacto de notificación:", error);
    return { statusCode: 500, body: JSON.stringify({ message: "No se pudo resolver el contacto" }) };
  }
};
