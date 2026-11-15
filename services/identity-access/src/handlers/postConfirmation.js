const {
  CognitoIdentityProviderClient,
  AdminAddUserToGroupCommand,
} = require("@aws-sdk/client-cognito-identity-provider");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, PutCommand } = require("@aws-sdk/lib-dynamodb");

const cognito = new CognitoIdentityProviderClient({});
const ddbDocClient = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  if (event.triggerSource !== "PostConfirmation_ConfirmSignUp") {
    return event;
  }

  const { userPoolId, userName } = event;
  const attrs = event.request.userAttributes;
  const email = attrs.email;

  const adminEmails = (process.env.ADMIN_EMAILS || "")
    .split(",")
    .map((e) => e.trim().toLowerCase());

  const rolAsignado = adminEmails.includes(email.toLowerCase())
    ? "admin"
    : "clientes";

  await cognito.send(
    new AdminAddUserToGroupCommand({
      UserPoolId: userPoolId,
      Username: userName,
      GroupName: rolAsignado,
    })
  );

  await ddbDocClient.send(
    new PutCommand({
      TableName: process.env.USERS_TABLE,
      Item: {
        userId: attrs.sub,
        email: email,
        dni: attrs["custom:dni"] || null,
        phone: attrs.phone_number || null,
        role: rolAsignado,
        createdAt: new Date().toISOString(),
      },
    })
  );

  return event;
};