# Reportes e incidentes: flujo integrado

Arquitectura, métricas por múltiples zonas y señal de antenas recurrentes: [`../ARQUITECTURA_Y_OPERACION.md`](../ARQUITECTURA_Y_OPERACION.md).

`incidents-report` (05) es el propietario can?nico de reportes e incidentes. Se integr? `feature/access-ia-incidents` en `integration/access-ia-full-flow`, conservando la cola com?n, el outbox y las conexiones de 03/04/08/09. `MICROSERVICIO 7` es legado opcional; el despliegue inicial no lo activa.

## API p?blica

Todas las rutas requieren Cognito del User Pool de Identity. Un cliente solo consulta sus reportes; el personal usa los grupos admin, supervisor_red, operador_soporte o tecnico_campo.

| M?todo | Ruta | Uso |
|---|---|---|
| POST | `/incidencias` | Registrar reporte propio; HTTP 202 |
| GET | `/incidencias` | Reportes propios; personal puede filtrar `zona`; colectivos con `tipo=colectivos&zona=...` |
| GET | `/incidencias/{id}` | Detalle e historial paginado autorizado |
| PATCH | `/incidencias/{id}/estado` | Personal; comando as?ncrono y verificaci?n de recuperaci?n |
| GET | `/incidencias/usuario/{userId}` | Due?o o personal; `limit/cursor` |
| GET | `/incidencias/zona/{zona}` | Personal; `limit/cursor` |
| GET | `/incidencias/metricas?zona=...` | Personal; `from/to`, hasta 31 d?as y 10000 filas por tabla |
| POST | `/soporte/chat` | Proxy Cognito hacia el chat privado de 06 |
| GET | `/v1/reports/incidents/{id}/recipients` | **AWS_IAM**, uso exclusivo de integraciones |

Ejemplo de registro:

```json
{"numeroServicio":"DEMO-001","zona":"ZONE-001","fechaHora":"2026-11-15T12:30:00.000Z","tipoProblema":"NO_SIGNAL","descripcion":"Sin se?al"}
```

`Idempotency-Key` permite reintentar el registro sin crear otro reporte. `userId` se toma del token, no del body. La evidencia opcional acepta im?genes JPEG/PNG/WebP base64 de hasta 60000 caracteres. No se acepta telemetr?a del cliente como fuente confiable.

## Eventos y persistencia

03 y 08 publican eventos persistidos a `rtc-events-dev`. EventBridge entrega las entradas a `rtc-incidents-input-dev`; ?nicamente 05 consume esa cola. Los comandos propios tambi?n se env?an mediante outbox. Se responde con fallos parciales SQS y se deduplica cada evento.

Diez usuarios distintos, mismo tipo de problema y zona en 24 horas activan la detecci?n colectiva por reportes. Una anomal?a t?cnica puede iniciar el flujo desde 03. Se consulta 04 para ubicar las antenas y 03 para su ?ltima medici?n mediante IAM/SigV4. Los campos son `antenna_id` y `traffic_gb_min`.

El incidente y su evento/historial se guardan transaccionalmente. Un publicador de outbox sincroniza estados/destinatarios y publica EventBridge; 09 consulta los destinatarios en este servicio y los contactos verificados en Identity. Los reportes a?adidos a un incidente abierto tambi?n reciben la resoluci?n.

08 confirma la asignaci?n antes de pasar a atenci?n. Resolver exige todas las antenas conocidas NORMAL y la orden COMPLETED si fue solicitada. Sin antenas conocidas, se necesita la orden completada; CANCELLED no equivale a recuperaci?n.

Tablas principales: `telecorp-incidents-service-reportes-dev`, `telecorp-incidents-service-colectivos-dev`, `telecorp-incidents-service-historial-dev`; cifrado, PITR y Retain declarados. Se conservan los ?ndices anteriores y se a?ade `IncidentReportesIndex`. Se definen tambi?n `...-state-dev`, `...-outbox-dev` y `...-recipients-dev`. No se cambian claves primarias existentes. Los l?mites actuales son 30 antenas y 1000 reportes por operaci?n.

## Desplegar desde Linux/EC2

Desde `services`, con AWS CLI v2, Node.js 22+, npm, Python 3.10+, uv y Serverless v4:

```bash
cp deploy_all.env.example deploy_all.env
chmod 600 deploy_all.env
nano deploy_all.env
serverless login
bash deploy_all.sh check
bash deploy_all.sh package
bash deploy_all.sh deploy
```

Completar `FRONTEND_ORIGIN` y `ADMIN_EMAILS`. Cuenta AWS `573505212581`, regi?n `us-east-1`, organizaci?n `santiagomorales`; `LabRole` debe confiar en Lambda y Step Functions y tener permisos de ejecuci?n, adem?s de los permisos de despliegue del operador. El Bash comprueba cuenta, confianza, bus y propiedad/esquema de `redtelecorp-antennas`. No modifica permisos, importa tablas ni elimina stacks.

Se despliegan siete stacks can?nicos y se pasan sus URLs/IDs reales; 03 se actualiza despu?s de 02. Las salidas y el entorno Postman quedan en `.deploy-all-output/`. `DEPLOY_LEGACY_REPORTS=true` permite desplegar 07 solo durante migraci?n. La colecci?n `postman/RedTeleCorp.full-flow.postman_collection.json` crea reportes en 05.

## Conexiones externas

| Proveedor | Crear/configurar | Variables locales de despliegue |
|---|---|---|
| Google Cloud | Cloud Run de 06 privado, Vertex AI con modelo habilitado, Secret Manager, cuenta invocadora y WIF para AWS LabRole | `AI_ENABLED=true`, `GCP_AI_BASE_URL`, `GCP_AUTH_MODE=WIF`, `GCP_WIF_AUDIENCE`, `GCP_INVOKER_SERVICE_ACCOUNT` |
| Clave IA AWS | SSM SecureString `/telecorp/dev/gcp/ai-api-key` con el mismo valor que `API_SECRET_TOKEN` de Cloud Run | `GCP_AI_API_KEY_PARAM`; opcionalmente `GCP_AI_SECRET_ARN` JSON `{"api_key":"..."}` tiene precedencia |
| Oracle Cloud | Autonomous Database con Spatial, usuario de aplicaci?n, conexi?n TCPS sin wallet, ACL para NAT fijo de AWS | `ORACLE_ENABLED=true`, `ORACLE_SECRET_ARN`, `ORACLE_SECURITY_GROUP_IDS`, `ORACLE_SUBNET_IDS` |
| Email/SMS AWS | SES verificado y SNS SMS habilitado; revisar sandbox, cuotas y permisos | `DELIVERY_ENABLED=true`, `SES_FROM_ADDRESS`, `SES_IDENTITY` |

WIF obtiene un token STS de Google a partir de la identidad AWS y solicita un ID token para la audiencia Cloud Run. Conceder `roles/iam.workloadIdentityUser` al principal externo en la cuenta invocadora y `roles/run.invoker` a esa cuenta sobre Cloud Run. No se utilizan claves JSON de cuentas de servicio. Las tres rutas de 06 exigen tambi?n `x-api-key`; el chat se accede mediante el proxy Cognito de 05. [WIF AWS](https://docs.cloud.google.com/iam/docs/workload-identity-federation-with-other-clouds) y [Cloud Run privado](https://docs.cloud.google.com/run/docs/authenticating/service-to-service).

06 necesita valores reales de `GCP_PROJECT`, `GCP_LOCATION` y `MODEL_ID`; desplegar con `--no-allow-unauthenticated` e inyectar `API_SECRET_TOKEN` desde GCP Secret Manager. Una indisponibilidad temporal de IA tiene respaldo marcado `RULES_AI_UNAVAILABLE`; errores de credenciales provocan reintentos.

04 conecta directamente con Oracle mediante `python-oracledb` y secreto `{"username":"...","password":"...","dsn":"tcps://HOST:PUERTO/SERVICIO"}`. Env?a GeoJSON como CLOB y recibe ?reas mediante Spatial; no necesita un endpoint ORDS ni tablas OCI inventadas. El YAML aplica VPC a ambas Lambdas de impacto; las subredes/NAT y la ACL deben configurarse realmente. [Conexi?n Oracle TLS](https://docs.oracle.com/en-us/iaas/autonomous-database-serverless/doc/connecting-python-tls.html).

Para la primera prueba, mantener IA/Oracle/entregas externas desactivados y configurar preferencias IN_APP=true, EMAIL/SMS=false. El c?lculo espacial local es funcional. Registrar/confirmar el correo administrador en Cognito, iniciar sesi?n y pegar su IdToken en el entorno Postman. Esperar SQS, Streams y Step Functions antes de evaluar los estados finales.

La gu?a local detallada est? en `services/docs/CONEXIONES_NUBES_EXTERNAS_Y_DEPLOY.md`; no se versiona por la pol?tica de archivos del proyecto. No se han creado recursos AWS/GCP/OCI ni probado proveedores reales durante la integraci?n.
