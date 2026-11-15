const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, GetCommand } = require("@aws-sdk/lib-dynamodb");

const ddbDocClient = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    const claims = event.requestContext.authorizer?.claims || {};
    const userId = claims.sub;

    if (!userId) {
      return {
        statusCode: 401,
        headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN || "" },
        body: JSON.stringify({ message: "Token inválido o sin identificador de usuario" }),
      };
    }

    const result = await ddbDocClient.send(
      new GetCommand({
        TableName: process.env.USERS_TABLE,
        Key: { userId },
      })
    );

    const gruposToken = claims["cognito:groups"] || [];

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN || "" },
      body: JSON.stringify({
        user: result.Item || {
          userId,
          email: claims.email,
          dni: claims["custom:dni"] || null,
          role: Array.isArray(gruposToken) ? gruposToken[0] : gruposToken,
        },
        cognitoGroups: gruposToken,
      }),
    };
  } catch (error) {
    console.error("identity_profile_failed", error.name);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN || "" },
      body: JSON.stringify({ message: "Error interno del servidor" }),
    };
  }
};