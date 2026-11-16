# Network Intelligence 06 ? Google Cloud Run + Vertex AI

Servicio privado invocado por 05. `/api/v1/ai/prioritize`, `/api/v1/ai/diagnose-incident` y `/api/v1/ai/chat` requieren `x-api-key`. El frontend usa `/soporte/chat` de 05 con Cognito; no llama directamente a este servicio.

Configurar valores reales de `GCP_PROJECT`, `GCP_LOCATION` y `MODEL_ID`. `API_SECRET_TOKEN` se inyecta desde Google Secret Manager; sin clave responde 503, y un valor incorrecto responde 401. No se incluyen claves ni nombres supuestos de modelos/proyectos.

```bash
gcloud run deploy redtelecorp-ai \
  --project "$GCP_PROJECT" --region "$GCP_REGION" \
  --source . --service-account "$AI_RUNTIME_SERVICE_ACCOUNT" \
  --no-allow-unauthenticated \
  --set-env-vars "GCP_PROJECT=$GCP_PROJECT,GCP_LOCATION=$VERTEX_LOCATION,MODEL_ID=$MODEL_ID" \
  --set-secrets "API_SECRET_TOKEN=$GCP_SECRET_NAME:$GCP_SECRET_VERSION"
```

Crear los recursos antes de ejecutar ese comando en la cuenta autorizada. La cuenta de ejecuci?n necesita acceso al modelo Vertex AI y `roles/secretmanager.secretAccessor` sobre el secreto. La cuenta invocadora usa `roles/run.invoker`; el proveedor WIF debe admitir ?nicamente la cuenta AWS 573505212581 y LabRole. 05 obtiene un ID token para Cloud Run y env?a tambi?n la clave, le?da de AWS SSM SecureString por defecto.

Consultar `../incidents-report/README.md` para el despliegue integrado. [Cloud Run privado](https://docs.cloud.google.com/run/docs/authenticating/service-to-service), [WIF AWS](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds), [secretos Cloud Run](https://docs.cloud.google.com/run/docs/configuring/services/secrets).

La comprobaci?n local usa proveedores simulados. Falta configurar y verificar el proyecto/modelo, WIF, cuentas y servicio reales; no se afirma que est?n desplegados.
