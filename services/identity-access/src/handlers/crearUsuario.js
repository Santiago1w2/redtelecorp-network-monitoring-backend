const {
  CognitoIdentityProviderClient,
  AdminCreateUserCommand,
  AdminSetUserPasswordCommand,
  AdminAddUserToGroupCommand,
} = require("@aws-sdk/client-cognito-identity-provider");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, PutCommand } = require("@aws-sdk/lib-dynamodb");

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
    let gruposCaller = claims['cognito:groups'] || [];
    if (typeof gruposCaller === 'string') { try { gruposCaller = JSON.parse(gruposCaller); } catch (_) { gruposCaller = gruposCaller.replace(/^\[|\]$/g, '').split(','); } }
    gruposCaller = Array.isArray(gruposCaller) ? gruposCaller.map(group => typeof group === 'string' ? group.trim() : '') : [];

    if (!gruposCaller.includes("admin")) {
      return {
        statusCode: 403,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({ message: "Acceso denegado: Se requiere rol admin" }),
      };
    }

    const { email, password, dni, phone, role } = JSON.parse(event.body || "{}");

    if (!email || !password || !ROLES_VALIDOS.includes(role)) {
      return {
        statusCode: 400,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          message: "Faltan campos obligatorios (email, password, role) o rol inválido",
          rolesPermitidos: ROLES_VALIDOS,
        }),
      };
    }

    const userPoolId = process.env.USER_POOL_ID;

    const userAttributes = [
      { Name: "email", Value: email },
      { Name: "email_verified", Value: "true" },
    ];
    if (dni) userAttributes.push({ Name: "custom:dni", Value: dni });
    if (phone) userAttributes.push({ Name: "phone_number", Value: phone });

    const createRes = await cognito.send(
      new AdminCreateUserCommand({
        UserPoolId: userPoolId,
        Username: email,
        UserAttributes: userAttributes,
        MessageAction: "SUPPRESS",
      })
    );

    const userId = createRes.User.Attributes.find((attr) => attr.Name === "sub")?.Value;

    await cognito.send(
      new AdminSetUserPasswordCommand({
        UserPoolId: userPoolId,
        Username: email,
        Password: password,
        Permanent: false, // Obliga a cambiar contraseña en el primer login
      })
    );

    await cognito.send(
      new AdminAddUserToGroupCommand({
        UserPoolId: userPoolId,
        Username: email,
        GroupName: role,
      })
    );

    await ddbDocClient.send(
      new PutCommand({
        TableName: process.env.USERS_TABLE,
        Item: {
          userId,
          email,
          dni: dni || null,
          phone: phone || null,
          role,
          createdBy: claims.email,
          createdAt: new Date().toISOString(),
        },
      })
    );

    return {
      statusCode: 201,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        message: "Usuario creado con contraseña temporal y asignado exitosamente",
        user: {
          userId,
          email,
          dni: dni || null,
          phone: phone || null,
          role,
        },
      }),
    };
  } catch (error) {
    console.error("Error creando usuario:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error al crear el usuario", error: error.message }),
    };
  }
};