const {
  CognitoIdentityProviderClient,
  AdminListGroupsForUserCommand,
  AdminRemoveUserFromGroupCommand,
  AdminAddUserToGroupCommand,
} = require("@aws-sdk/client-cognito-identity-provider");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, UpdateCommand } = require("@aws-sdk/lib-dynamodb");

const cognito = new CognitoIdentityProviderClient({});
const ddbDocClient = DynamoDBDocumentClient.from(new DynamoDBClient({}));

const ROLES_VALIDOS = [
  "admin",
  "supervisor_red",
  "tecnico_campo",
  "operador_soporte",
  "clientes",
];

exports.handler = async (event) => {
  try {
    const claims = event.requestContext.authorizer?.claims || {};
    const gruposCaller = claims["cognito:groups"] || "";

    if (!gruposCaller.includes("admin")) {
      return {
        statusCode: 403,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: "Acceso denegado: Se requiere rol admin" }),
      };
    }

    const { userId, email, nuevoRol } = JSON.parse(event.body || "{}");

    if (!userId || !email || !ROLES_VALIDOS.includes(nuevoRol)) {
      return {
        statusCode: 400,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          message: "Parámetros inválidos",
          rolesPermitidos: ROLES_VALIDOS,
        }),
      };
    }

    const userPoolId = process.env.USER_POOL_ID;

    const gruposActuales = await cognito.send(
      new AdminListGroupsForUserCommand({
        UserPoolId: userPoolId,
        Username: email,
      })
    );

    for (const grupo of gruposActuales.Groups || []) {
      await cognito.send(
        new AdminRemoveUserFromGroupCommand({
          UserPoolId: userPoolId,
          Username: email,
          GroupName: grupo.GroupName,
        })
      );
    }

    await cognito.send(
      new AdminAddUserToGroupCommand({
        UserPoolId: userPoolId,
        Username: email,
        GroupName: nuevoRol,
      })
    );

    await ddbDocClient.send(
      new UpdateCommand({
        TableName: process.env.USERS_TABLE,
        Key: { userId },
        UpdateExpression: "set #r = :rol, updatedAt = :fecha",
        ExpressionAttributeNames: { "#r": "role" },
        ExpressionAttributeValues: {
          ":rol": nuevoRol,
          ":fecha": new Date().toISOString(),
        },
      })
    );

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        message: `Rol actualizado exitosamente a ${nuevoRol}`,
        userId,
        email,
        role: nuevoRol,
      }),
    };
  } catch (error) {
    console.error("Error asignando rol:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error interno al asignar rol", error: error.message }),
    };
  }
};