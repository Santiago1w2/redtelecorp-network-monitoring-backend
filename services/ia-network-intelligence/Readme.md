# Red TeleCorp — MS6: AI Network Intelligence

Microservicio de Inteligencia Artificial desplegado en **Google Cloud Run** con **Vertex AI (`gemini-3.5-flash-lite`)** para la plataforma híbrida de telecomunicaciones Red TeleCorp.

## Endpoints

| Método | Ruta | Auth (`x-api-key`) | Descripción |
| :--- | :--- | :---: | :--- |
| `GET` | `/` | No | Health check del servicio y estado multimodal. |
| `POST` | `/api/v1/ai/prioritize` | Sí | Triaje de reportes individuales (texto e imagen Base64) y detección de infraestructura crítica. |
| `POST` | `/api/v1/ai/diagnose-incident` | Sí | Correlación masiva de reportes zonales y métricas de antenas para diagnóstico de causa raíz. |
| `POST` | `/api/v1/ai/chat` | No | Asistente virtual público de soporte técnico y recolección de datos de incidencia. |

## Variables de Entorno

- `API_SECRET_TOKEN`: Token interno de autenticación entre microservicios (obligatorio; mismo valor protegido que el secreto AWS de 05).
- `GCP_PROJECT`: ID del proyecto en Google Cloud (`red-telecorp`).
- `GCP_LOCATION`: Región del endpoint de Vertex AI (`global`).
- `MODEL_ID`: Modelo generativo utilizado (`gemini-3.5-flash-lite`).

## Despliegue en Google Cloud Run

```bash
gcloud run deploy ms6-ai-intelligence \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-secrets API_SECRET_TOKEN=NOMBRE_SECRETO_GCP:1 \
  --set-env-vars GCP_PROJECT="ID_REAL_DEL_PROYECTO",GCP_LOCATION="global"
```

Reemplazar el proyecto, el secreto y su versión por recursos reales. La cuenta de servicio de Cloud Run necesita acceso a Vertex AI y `roles/secretmanager.secretAccessor` sobre ese secreto. 05 envía el mismo valor mediante `x-api-key`, leído de AWS Secrets Manager; no guardarlo en Git. Sin `API_SECRET_TOKEN`, las rutas internas de IA responden 503. [Configuración oficial de secretos](https://docs.cloud.google.com/run/docs/configuring/services/secrets).
