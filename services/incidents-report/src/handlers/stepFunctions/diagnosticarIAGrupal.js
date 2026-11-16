const { SignatureV4 } = require('@smithy/signature-v4');
const { HttpRequest } = require('@smithy/protocol-http');
const { Sha256 } = require('@aws-crypto/sha256-js');
const { defaultProvider } = require('@aws-sdk/credential-provider-node');
let cachedKey, keyExpires = 0, cachedIdToken, idExpires = 0;
async function aiKey() {
  if (cachedKey && Date.now() < keyExpires) return cachedKey;
  if (process.env.GCP_AI_SECRET_ARN) {
    const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager');
    const value = await new SecretsManagerClient({}).send(new GetSecretValueCommand({ SecretId: process.env.GCP_AI_SECRET_ARN }));
    cachedKey = JSON.parse(value.SecretString).api_key;
  } else {
    if (!process.env.GCP_AI_API_KEY_PARAM) throw new Error('AISecretMissing');
    const { SSMClient, GetParameterCommand } = require('@aws-sdk/client-ssm');
    const value = await new SSMClient({}).send(new GetParameterCommand({ Name: process.env.GCP_AI_API_KEY_PARAM, WithDecryption: true }));
    cachedKey = value.Parameter?.Value;
  }
  if (typeof cachedKey !== 'string' || !cachedKey) throw new Error('AISecretInvalid');
  keyExpires = Date.now() + 300000;
  return cachedKey;
}
async function cloudRunToken(audience) {
  if (cachedIdToken && Date.now() < idExpires) return cachedIdToken;
  const providerAudience = process.env.GCP_WIF_AUDIENCE;
  const account = process.env.GCP_INVOKER_SERVICE_ACCOUNT;
  if (!/^\/\/iam\.googleapis\.com\/projects\/\d+\/locations\/global\/workloadIdentityPools\/[\w-]+\/providers\/[\w-]+$/.test(providerAudience || '')
    || !/^[\w.-]+@[\w.-]+\.iam\.gserviceaccount\.com$/.test(account || '')) throw new Error('WIFConfigurationMissing');
  const { AwsClient } = require('google-auth-library');
  const auth = new AwsClient({ audience: providerAudience, subject_token_type: 'urn:ietf:params:aws:token-type:aws4_request', token_url: 'https://sts.googleapis.com/v1/token',
    scopes: ['https://www.googleapis.com/auth/cloud-platform'], aws_security_credentials_supplier: {
      getAwsRegion: async () => process.env.AWS_REGION || 'us-east-1',
      getAwsSecurityCredentials: async () => { const value = await defaultProvider()(); return { accessKeyId: value.accessKeyId, secretAccessKey: value.secretAccessKey, token: value.sessionToken }; },
    } });
  const subject = await auth.retrieveSubjectToken();
  const exchange = await fetch('https://sts.googleapis.com/v1/token', {
    method: 'POST', headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ grant_type: 'urn:ietf:params:oauth:grant-type:token-exchange', audience: providerAudience,
      requested_token_type: 'urn:ietf:params:oauth:token-type:access_token', subject_token_type: 'urn:ietf:params:aws:token-type:aws4_request',
      subject_token: subject, scope: 'https://www.googleapis.com/auth/cloud-platform' }), signal: AbortSignal.timeout(8000),
  });
  if (!exchange.ok) { const error = new Error('WIFExchangeHTTP' + exchange.status); error.statusCode = [400, 401, 403].includes(exchange.status) ? 403 : exchange.status; throw error; }
  const access = await exchange.json();
  if (typeof access.access_token !== 'string' || !access.access_token) throw new Error('WIFAccessTokenInvalid');
  const response = await fetch('https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/' + encodeURIComponent(account) + ':generateIdToken', {
    method: 'POST', headers: { Authorization: 'Bearer ' + access.access_token, 'Content-Type': 'application/json' },
    body: JSON.stringify({ audience, includeEmail: true }), signal: AbortSignal.timeout(8000),
  });
  if (!response.ok) { const error = new Error('WIFTokenHTTP' + response.status); error.statusCode = response.status; throw error; }
  const value = await response.json();
  if (typeof value.token !== 'string' || value.token.split('.').length !== 3) throw new Error('WIFTokenInvalid');
  cachedIdToken = value.token;
  idExpires = Math.min(Date.now() + 3000000, Number(JSON.parse(Buffer.from(value.token.split('.')[1], 'base64url').toString()).exp) * 1000 - 60000);
  return cachedIdToken;
}
async function httpJson(base, route, method = 'GET', data, ai = false) {
  if (!base) throw new Error('IntegrationUrlMissing');
  const url = new URL(base.replace(/\/$/, '') + route);
  if (url.protocol !== 'https:' && !(process.env.LOCAL_TEST === 'true' && ['127.0.0.1', 'localhost'].includes(url.hostname))) throw new Error('HTTPSRequired');
  const body = data === undefined ? undefined : JSON.stringify(data);
  let signedHeaders = { 'content-type': 'application/json', host: url.host };
  if (ai) {
    signedHeaders['x-api-key'] = await aiKey();
    const mode = process.env.GCP_AUTH_MODE || 'WIF';
    if (mode === 'WIF') signedHeaders.Authorization = 'Bearer ' + await cloudRunToken(new URL(base).origin);
    else if (mode !== 'API_KEY') throw new Error('InvalidGCPAuthMode');
  } else {
    const signer = new SignatureV4({ credentials: defaultProvider(), region: process.env.AWS_REGION || 'us-east-1', service: 'execute-api', sha256: Sha256 });
    signedHeaders = (await signer.sign(new HttpRequest({ protocol: url.protocol, hostname: url.hostname,
      port: url.port ? Number(url.port) : undefined, method, path: url.pathname, query: Object.fromEntries(url.searchParams), headers: signedHeaders, body }))).headers;
  }
  const response = await fetch(url, { method, headers: signedHeaders, body, signal: AbortSignal.timeout(ai ? 15000 : 12000) });
  if (!response.ok) { if (ai && [401, 403].includes(response.status)) { cachedKey = undefined; cachedIdToken = undefined; } const error = new Error('IntegrationHTTP' + response.status); error.statusCode = response.status; throw error; }
  return response.json();
}
exports.handler = async event => {
  let diagnosticoIA;
  if (process.env.AI_ENABLED === 'true') {
    diagnosticoIA = await httpJson(process.env.GCP_AI_BASE_URL, '/api/v1/ai/diagnose-incident', 'POST',
      { zona: event.zona, reportes: event.descripciones, antenas_estado: event.antenasNormalizadas }, true);
    if (typeof diagnosticoIA.requiere_cuadrilla_campo !== 'boolean' || !['BAJO', 'MEDIO', 'ALTO'].includes(diagnosticoIA.riesgo_estructural)
      || typeof diagnosticoIA.causa_raiz_probable !== 'string' || typeof diagnosticoIA.mensaje_notificacion_usuario !== 'string') throw new Error('InvalidAIContract');
    diagnosticoIA.analysis_method = 'GCP_VERTEX_AI';
  } else {
    diagnosticoIA = { causa_raiz_probable: 'Diagnostico IA desactivado; incidente detectado por reglas de reportes/telemetria.',
      requiere_cuadrilla_campo: true, riesgo_estructural: 'MEDIO', mensaje_notificacion_usuario: 'Nuestro equipo evalua la incidencia.', analysis_method: 'RULES_AI_DISABLED' };
  }
  if (event.recurrencia) {
    diagnosticoIA.requiere_cuadrilla_campo = true;
    diagnosticoIA.riesgo_estructural = 'ALTO';
    diagnosticoIA.causa_raiz_probable = `Falla recurrente: ${event.recurrencia.failure_events} eventos en ${event.recurrencia.affected_days} dias dentro de ${event.recurrencia.window_days} dias. ${diagnosticoIA.causa_raiz_probable}`.slice(0, 1500);
    diagnosticoIA.analysis_method = `${diagnosticoIA.analysis_method || 'RULES'}_RECURRENCE_RULE`;
  }
  return { ...event, diagnosticoIA };
};
