const { SSMClient, GetParameterCommand } = require("@aws-sdk/client-ssm");
const ssm = new SSMClient({});
let cachedValue;
let expiresAt = 0;

exports.getAiApiKey = async () => {
  if (cachedValue && Date.now() < expiresAt) return cachedValue;
  const name = process.env.GCP_AI_API_KEY_PARAM;
  if (!name) throw new Error("Falta GCP_AI_API_KEY_PARAM");
  const result = await ssm.send(new GetParameterCommand({ Name: name, WithDecryption: true }));
  const value = result.Parameter?.Value;
  if (!value) throw new Error("SSM no devolvió la clave del servicio IA");
  cachedValue = value;
  expiresAt = Date.now() + 5 * 60 * 1000;
  return cachedValue;
};
