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

- `API_SECRET_TOKEN`: Token interno de autenticación entre microservicios (por defecto `telecorp-secret-2026`).
- `GCP_PROJECT`: ID del proyecto en Google Cloud (`red-telecorp`).
- `GCP_LOCATION`: Región del endpoint de Vertex AI (`global`).
- `MODEL_ID`: Modelo generativo utilizado (`gemini-3.5-flash-lite`).

## Despliegue en Google Cloud Run

```bash
gcloud run deploy ms6-ai-intelligence \
  --source . \
  --region us-central1 \
  --allow-unauthenticated \
  --set-env-vars API_SECRET_TOKEN="telecorp-secret-2026",GCP_PROJECT="red-telecorp",GCP_LOCATION="global"