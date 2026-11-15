
const { SignatureV4 } = require('@smithy/signature-v4');
const { HttpRequest } = require('@smithy/protocol-http');
const { Sha256 } = require('@aws-crypto/sha256-js');
const { defaultProvider } = require('@aws-sdk/credential-provider-node');
const { SecretsManagerClient, GetSecretValueCommand } = require('@aws-sdk/client-secrets-manager');
const secrets = new SecretsManagerClient({});
let cachedKey;
async function httpJson(base, route, method = 'GET', data, ai = false) {
  if (!base) throw new Error('IntegrationUrlMissing');
  const url = new URL(base.replace(/\/$/, '') + route);
  if (url.protocol !== 'https:' && !(process.env.LOCAL_TEST === 'true' && ['127.0.0.1', 'localhost'].includes(url.hostname))) throw new Error('HTTPSRequired');
  const body = data === undefined ? undefined : JSON.stringify(data);
  let headers = { 'content-type': 'application/json', host: url.host };
  if (ai) {
    if (!cachedKey) {
      if (!process.env.GCP_AI_SECRET_ARN) throw new Error('AISecretMissing');
      const value = await secrets.send(new GetSecretValueCommand({ SecretId: process.env.GCP_AI_SECRET_ARN }));
      cachedKey = JSON.parse(value.SecretString).api_key;
      if (typeof cachedKey !== 'string' || !cachedKey) throw new Error('AISecretInvalid');
    }
    headers['x-api-key'] = cachedKey;
  } else {
    const signer = new SignatureV4({ credentials: defaultProvider(), region: process.env.AWS_REGION || 'us-east-1', service: 'execute-api', sha256: Sha256 });
    headers = (await signer.sign(new HttpRequest({ protocol: url.protocol, hostname: url.hostname,
      port: url.port ? Number(url.port) : undefined, method, path: url.pathname + url.search, headers, body }))).headers;
  }
  const response = await fetch(url, { method, headers, body, signal: AbortSignal.timeout(ai ? 15000 : 12000) });
  if (!response.ok) { if (ai && response.status === 401) cachedKey = undefined; const error = new Error('IntegrationHTTP' + response.status); error.statusCode = response.status; throw error; }
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
  return { ...event, diagnosticoIA };
};
