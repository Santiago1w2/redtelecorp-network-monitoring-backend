# Red TeleCorp — IA Network Intelligence

Servicio de análisis desplegado en Google Cloud Run con Vertex AI.

## Endpoints

| Método | Ruta | Autenticación | Uso |
|---|---|---|---|
| GET | `/` | Pública | Health check |
| POST | `/api/v1/ai/prioritize` | `x-api-key` | Clasificar y priorizar reportes |
| POST | `/api/v1/ai/diagnose-incident` | `x-api-key` | Diagnosticar afectaciones colectivas |
| POST | `/api/v1/ai/chat` | Pública | Asistente de soporte |

`API_SECRET_TOKEN` es obligatorio; no hay clave predeterminada. Configura el mismo secreto en Google Secret Manager y en AWS SSM Parameter Store como `SecureString` en `/telecorp/<stage>/gcp/ai-api-key`. `incidents-report` lo lee desde SSM en tiempo de ejecución, con caché breve. Cognito sigue siendo la autoridad de login y autenticación de usuarios en las APIs de AWS.

## Despliegue

Guarda primero la clave en Google Secret Manager y habilita Vertex AI:

```bash
gcloud run deploy ms6-ai-intelligence \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets API_SECRET_TOKEN=telecorp-ai-api-key:latest \
  --set-env-vars GCP_PROJECT=red-telecorp,GCP_LOCATION=global
```

Las rutas de priorización y diagnóstico requieren `x-api-key`. La URL de Cloud Run queda invocable públicamente porque aún no se configuró Workload Identity Federation entre AWS y Google; no la expongas al frontend. El endpoint `/api/v1/ai/chat` también es público: antes de producción, expónlo mediante un proxy AWS protegido por Cognito. Configura `/telecorp/<stage>/gcp/ai-base-url` en SSM con la URL HTTPS del servicio.
