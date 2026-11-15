# Flujo de reportes e incidencias

`incidents-report` es el servicio canónico para registrar y administrar reportes. El login, registro y emisión de grupos siguen en Cognito (`identity-access`); este servicio valida el token con el mismo User Pool publicado en SSM.

## Endpoints

Todas las rutas de usuario requieren un JWT de Cognito en `Authorization: Bearer <token>`.

| Método | Ruta | Acceso |
|---|---|---|
| POST | `/incidencias` | Usuario autenticado; crea un reporte propio |
| GET | `/incidencias` | Clientes ven lo propio; personal interno puede listar o filtrar con `?zona=` |
| GET | `/incidencias/{id}` | Dueño del reporte o personal interno; incluye historial |
| PATCH | `/incidencias/{id}/estado` | Personal interno autorizado |
| GET | `/incidencias/usuario/{userId}` | Dueño o personal interno |
| GET | `/incidencias/zona/{zona}` | Personal interno |
| GET | `/incidencias/metricas` | Personal interno |
| GET | `/v1/reports/incidents/{id}/recipients` | AWS IAM; integración privada con notificaciones |

El servicio detecta diez usuarios diferentes con el mismo tipo de problema en una zona durante 24 horas, luego ejecuta Step Functions. La solicitud de cuadrilla se envía a Field Operations por EventBridge; el estado del incidente y las notificaciones se actualizan cuando Field Operations confirma asignación o cierre. Los cambios de estado se publican en el bus compartido para notificaciones. Si el diagnóstico IA falla, el flujo conserva el incidente para verificación operativa con un diagnóstico de respaldo.

## Despliegue

1. Desplegar `identity-access` primero. El stack publica el ARN e ID del User Pool y el ID de cliente en `/telecorp/<stage>/cognito/`.
2. Crear `/telecorp/<stage>/gcp/ai-base-url` como parámetro SSM `String` y `/telecorp/<stage>/gcp/ai-api-key` como `SecureString`. El rol Lambda requiere `ssm:GetParameter` para ese ARN y `kms:Decrypt` si el parámetro usa una CMK.
3. Desplegar `network-telemetry` para crear el bus compartido `rtc-events-<stage>`.
4. Ejecutar `npm install` en este directorio para instalar `serverless-step-functions`, y luego `serverless deploy --stage <stage> --param="eventBusName=rtc-events-<stage>"`.
5. Configurar en notificaciones `REPORTS_API_URL` con el endpoint de este stack e `IDENTITY_API_URL` con el endpoint de identidad. Sus ARNs de API deben autorizar `execute-api:Invoke` desde el rol de notificaciones.

`MICROSERVICIO 7` queda como implementación anterior de reportes y no forma parte del flujo integrado. Migra el frontend a las rutas anteriores y evita desplegar ambas APIs como fuentes de verdad simultáneas.
