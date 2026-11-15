const { DynamoDBClient } = require('@aws-sdk/client-dynamodb');
const { DynamoDBDocumentClient, GetCommand } = require('@aws-sdk/lib-dynamodb');
const { CognitoIdentityProviderClient, AdminGetUserCommand } = require('@aws-sdk/client-cognito-identity-provider');
const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const cognito = new CognitoIdentityProviderClient({});

exports.handler = async (event) => {
  if (!event.requestContext?.identity?.userArn) {
    return { statusCode: 403, body: JSON.stringify({ message: 'Se requiere identidad IAM' }) };
  }
  const userId = event.pathParameters?.user_id;
  if (typeof userId !== 'string' || !/^[A-Za-z0-9_.:-]{1,128}$/.test(userId)) {
    return { statusCode: 400, body: JSON.stringify({ message: 'user_id invalido' }) };
  }
  try {
    const { Item } = await ddb.send(new GetCommand({ TableName: process.env.USERS_TABLE, Key: { userId }, ConsistentRead: true }));
    if (!Item?.email) return { statusCode: 404, body: JSON.stringify({ message: 'Usuario no encontrado' }) };
    const user = await cognito.send(new AdminGetUserCommand({ UserPoolId: process.env.USER_POOL_ID, Username: Item.email }));
    const attributes = Object.fromEntries((user.UserAttributes || []).map(attribute => [attribute.Name, attribute.Value]));
    if (attributes.sub !== userId) return { statusCode: 404, body: JSON.stringify({ message: 'Usuario no encontrado' }) };
    const result = { user_id: userId, active: user.Enabled === true && user.UserStatus === 'CONFIRMED' };
    if (attributes.email_verified === 'true') result.email = attributes.email;
    if (attributes.phone_number_verified === 'true') result.phone_e164 = attributes.phone_number;
    return { statusCode: 200, headers: { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }, body: JSON.stringify(result) };
  } catch (error) {
    if (error.name === 'UserNotFoundException') return { statusCode: 404, body: JSON.stringify({ message: 'Usuario no encontrado' }) };
    console.error('notification_contact_failed', error.name);
    return { statusCode: 503, body: JSON.stringify({ message: 'Identity temporalmente no disponible' }) };
  }
};
