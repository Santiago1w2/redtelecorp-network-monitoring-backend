# Despliegue desde EC2 Linux y pruebas en Postman

Los servicios se ejecutan en **Lambda**. EC2 es la máquina desde la que construyes los paquetes y ejecutas Serverless; no necesitas abrir puertos HTTP en EC2 para estas APIs. API Gateway publica los endpoints HTTPS.

## Cómo leer los serverless.yml

La estructura sigue las prácticas `semana10/lab2/api-alumnos`, `semana11/lab1/api-comentario` y `semana12/lab2/api-web-scraping-igp`, dentro de `ejemplos/semana10`:

| Bloque | Qué significa |
|---|---|
| `service` | Nombre del microservicio y prefijo del stack |
| `provider` | AWS, Python 3.12, memoria, timeout y rol LabRole |
| `provider.environment` | Variables que leen los handlers con `os.environ`: tablas, URLs, colas y configuración |
| `functions` | Una Lambda por archivo; `handler` señala el archivo y `lambda_handler` |
| `events.http` | Método y ruta HTTP que llama a esa Lambda |
| `custom.cors` / `custom.authorizer` | Configuración común del frontend y autenticación, reutilizada por las rutas |
| `package` | Qué archivos se suben; Coverage usa el ZIP que incluye sus librerías Linux |
| `resources.Resources` | Tablas, buckets, colas, topics y conexiones creados por CloudFormation |

Por ejemplo, `handler: handlers.post_telemetry.lambda_handler` ejecuta `lambda_handler` de `handlers/post_telemetry.py`. Su evento `http`, con `path: /v1/telemetry` y `method: post`, crea el endpoint POST correspondiente. `integration: lambda-proxy` permite que el cliente reciba el `statusCode` y `body` que devuelve ese handler; los ejemplos del curso con `integration: lambda` usan otra transformación de entrada/salida y no se copiaron literalmente.

Las expresiones también tienen una función concreta:

- `${param:frontendOrigin, env:FRONTEND_ORIGIN}` recibe el valor del comando Serverless o de una variable de entorno.
- `${self:provider.environment.LATEST_TABLE}` reutiliza un valor escrito en el mismo YAML, igual que `TABLE_NAME` en las prácticas.
- `${sls:stage}` coloca `dev` u otro stage en el nombre, evitando mezclar entornos.
- `{Ref: IngestQueue}` obtiene la URL de la cola que crea AWS. `{'Fn::GetAtt': [IngestQueue, Arn]}` obtiene su ARN para conectar el trigger.
- `Fn::Sub` completa textos con datos que resuelve AWS, como el identificador de cuenta o el ARN del bucket.

Los recursos de los tres servicios permanecen en sus respectivos YAML. Se quitaron parámetros CloudFormation redundantes y las políticas IAM que solo eran texto informativo. Se conservan los nombres de los recursos de negocio, la autenticación, el CORS restringido, los triggers, los reintentos y las colas de errores. Cada servicio mantiene una alarma en su consumidor principal, además de las alarmas de colas de errores. Las 23 alarmas adicionales por función se retiraron de la definición; una actualización de un stack que las tuviera desplegadas también retirará esas alarmas adicionales, conservando logs y las alarmas principales.

La memoria predeterminada es 256 MB, conservando el valor efectivo anterior. El análisis espacial mantiene 512 MB/60 segundos; los consumidores de notificaciones conservan sus timeouts específicos. No se cambiaron handlers ni contratos HTTP en esta simplificación. El Bash y la colección Postman usan las mismas rutas y parámetros de despliegue.

## 1. Preparar la EC2

Trabaja con una copia del repositorio que incluya los archivos de esta carpeta `services`. Necesitas Bash, Node.js 22 o 24 con npm, Python 3.10 o posterior, AWS CLI v2, Serverless Framework v4 y `uv`. El runtime de las Lambdas sigue siendo Python 3.12. El paquete de Coverage descarga explícitamente ruedas Linux x86_64/cp312, aunque la versión de Python de la EC2 sea diferente.

Instala Node siguiendo su [documentación oficial](https://nodejs.org/en/download) y AWS CLI siguiendo la [guía para Linux](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html). Con Node y Python disponibles, puedes instalar las herramientas del proyecto en tu usuario:

```bash
python3 -m venv "$HOME/.venvs/redtelecorp-tools"
source "$HOME/.venvs/redtelecorp-tools/bin/activate"
python -m pip install --upgrade pip uv
npm install --prefix "$HOME/.local/redtelecorp-tools" serverless@4
export PATH="$HOME/.local/redtelecorp-tools/node_modules/.bin:$PATH"

aws --version
node --version
python3 --version
uv --version
serverless --version
```

Si `venv` no está disponible, instala el paquete `python3-venv` de tu distribución. Reactiva el entorno virtual y exporta el PATH al iniciar una nueva sesión SSH. Framework v4 puede solicitar autenticación de Serverless: usa `serverless login` con acceso a la organización `santiagomorales`. La autenticación de Serverless es independiente de la de AWS; no guardes sus claves en Git.

Asocia a EC2 un IAM instance profile autorizado para desplegar estos stacks y consultar STS, CloudFormation y EventBridge. En AWS Academy, también puedes utilizar un perfil temporal de AWS CLI, con su session token vigente, y seleccionar `AWS_PROFILE` en el archivo local. No copies credenciales en el script. La identidad de despliegue necesita permisos de creación/actualización de recursos e `iam:PassRole` para el rol de ejecución.

El rol de ejecución de Lambda debe existir. Por defecto se usa `arn:aws:iam::<cuenta-autenticada>:role/LabRole`; puedes configurar `LAMBDA_ROLE_ARN`. El stack utiliza ese rol y no modifica sus políticas. Se retiró de los YAML el bloque informativo `custom.requiredIamPolicy`, que no concedía permisos. Un administrador debe autorizar los recursos concretos de cada servicio:

| Servicio | Acciones necesarias para sus Lambdas |
|---|---|
| 03 | DynamoDB `GetItem`, `PutItem`, `Query` y lectura de Streams; SQS enviar/recibir/borrar/consultar atributos; S3 `PutObject`; EventBridge `PutEvents`; `execute-api:Invoke` a Infrastructure 02 |
| 04 | DynamoDB `GetItem`, `PutItem`, `DeleteItem`, `BatchWriteItem`, `Query` y lectura de Streams; S3 `GetObject`/`PutObject`; SQS `SendMessage` al destino de fallos; `execute-api:Invoke` a Infrastructure 02; `secretsmanager:GetSecretValue` únicamente para el secreto Oracle configurado |
| 09 | DynamoDB `GetItem`, `PutItem`, `UpdateItem`, `Query` y lectura de Streams; SQS enviar/recibir/borrar/consultar atributos; `execute-api:Invoke` a Identity 01 y Reports 07; `ses:SendEmail` para la identidad verificada; `sns:Publish` para los SMS autorizados |

La lectura de Streams requiere `DescribeStream`, `GetRecords`, `GetShardIterator` y `ListStreams`. Los tres servicios necesitan permisos de logs y los consumidores de Streams necesitan `sqs:SendMessage` a su cola de errores. Las escrituras transaccionales de DynamoDB requieren las acciones de cada operación que integra la transacción. Limita las políticas a los ARN reales de tablas, índices, buckets, colas, bus, secreto y APIs; no se accede a tablas privadas de otros equipos.

## 2. Configurar y desplegar

Desde `services`:

```bash
cp deploy_ec2.env.example deploy_ec2.env
chmod 600 deploy_ec2.env
nano deploy_ec2.env
bash deploy_ec2.sh check all
bash deploy_ec2.sh package all
```

Completa `FRONTEND_ORIGIN` (origen HTTPS sin ruta/barra final). Las tres APIs utilizan `COGNITO_USER_POOLS`, igual que Identity, y reutilizan su User Pool existente. Por defecto leen su ARN del parámetro SSM `/telecorp/<stage>/cognito/user-pool-arn`, publicado por Identity. Despliega Identity primero y permite a la identidad de despliegue leer ese parámetro.

Si necesitas indicar el pool explícitamente, configura `COGNITO_USER_POOL_ARN` en `deploy_ec2.env`, o pasa `--param="cognitoUserPoolArn=<ARN_REAL_DEL_USER_POOL>"` al ejecutar Serverless directamente. Es un ARN `arn:aws:cognito-idp:...:userpool/...`; ya no se utiliza `IDENTITY_AUTHORIZER_ARN` ni una Lambda authorizer externa. El ARN puede pertenecer a la cuenta de Identity cuando sea distinta; en ese caso el valor SSM de esa otra cuenta no se descubre automáticamente y debes suministrar el ARN real.

API Gateway verifica el token de Cognito y entrega `sub` y `cognito:groups` en `requestContext.authorizer.claims`. Los handlers no decodifican ni aceptan JWT sin verificar enviados en el body. Se comprueba pertenencia exacta a los grupos, aceptando su representación como lista o texto de API Gateway. Los scopes personalizados anteriores no conceden acceso.

| Operación | Grupos permitidos |
|---|---|
| Leer telemetría y cobertura | `admin`, `supervisor_red`, `operador_soporte`, `tecnico_campo` |
| Ingresar telemetría y modificar cobertura/zonas | `admin`, `supervisor_red` |
| Analizar impacto | `admin`, `supervisor_red`, `operador_soporte` |
| Crear notificaciones manuales | `admin`, `operador_soporte` |
| Listar/leer/marcar notificaciones y guardar preferencias propias | Cualquier usuario autenticado; se conserva la restricción por `sub` |

`clientes` puede gestionar sus propias notificaciones; no recibe permisos internos de red ni de envío manual. Para estas REST APIs sin scopes OAuth configurados, usa el **IdToken** que devuelve Cognito al iniciar sesión, igual que el contrato de Identity; no necesitas crear otro User Pool ni un Resource Server de scopes. [Integración oficial de API Gateway y Cognito](https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-integrate-with-cognito.html).

Configura las URLs base de las APIs externas, sin `/v1` ni barra final:

| Variable | Contrato consumido |
|---|---|
| `INFRASTRUCTURE_API_URL` | `GET /v1/infrastructure/antennas/{antenna_id}`; devuelve `antenna_id` |
| `IDENTITY_API_URL` | `GET /v1/identity/users/{user_id}/notification-contact`; devuelve `user_id`, `active` y contactos autorizados |
| `REPORTS_API_URL` | `GET /v1/reports/incidents/{incident_id}/recipients?limit=100&cursor=...`; devuelve `recipient_ids`, `cursor` |

Las llamadas de Lambda a estas APIs se firman con SigV4: el equipo propietario debe exponer estos contratos y autorizar al rol. Configura los permisos del rol sobre los ARN reales de esas APIs. Las variables `*_API_ARN` del archivo de configuración son referencias para el administrador; no adjuntan políticas al rol. Si las URLs están vacías, el script avisa y permite crear la infraestructura; los endpoints dependientes no estarán operativos y devolverán error.

Para crear/actualizar recursos AWS facturables, ejecuta expresamente:

```bash
bash deploy_ec2.sh deploy all
```

También puedes elegir uno:

```bash
bash deploy_ec2.sh deploy telemetry
bash deploy_ec2.sh deploy coverage
bash deploy_ec2.sh deploy notifications
```

`all` despliega Telemetry primero, luego Coverage y Notifications. `CREATE_EVENT_BUS=auto` consulta el stack de Telemetry y el bus compartido: crea el bus con Telemetry si falta y conserva su propiedad en actualizaciones posteriores. Si despliegas Coverage o Notifications por separado, el bus debe existir. No cambies el nombre del bus en un entorno existente sin coordinar las integraciones.

`check` resuelve los YAML y consulta AWS; no crea recursos ni garantiza todas las autorizaciones. `package` genera paquetes/CloudFormation sin crear stacks y descarga dependencias de Coverage. `deploy` ejecuta Serverless y CloudFormation. Se detiene al primer fallo; no ejecuta `remove` ni elimina stacks. Serverless utiliza su bucket de despliegue, además de los buckets de negocio definidos. No crees manualmente recursos con los mismos nombres: un recurso existente fuera de estos stacks necesita importación/reutilización explícita.

Después de cada despliegue, las salidas reales se guardan en `.deploy-output/<servicio>-<stage>.json` y se muestra `ServiceEndpoint`. Esas URLs ya incluyen `/dev` (o el stage elegido). No añadas el stage otra vez en Postman.

## 3. ¿Serverless crea SQS y SNS?

**Sí, los YAML definen siete colas SQS y un topic SNS. Se crean al desplegar con permisos suficientes; escribir el YAML o ejecutar `package` no los crea.** Para `STAGE=dev`:

| Servicio | Tipo | Nombre definido | Uso |
|---|---|---|---|
| 03 | SQS | `rtc-telemetry-ingest-dev` | Cola de mediciones → `process_telemetry` |
| 03 | SQS | `rtc-telemetry-ingest-dlq-dev` | Mediciones fallidas tras 5 recepciones |
| 03 | SQS | `rtc-network-telemetry-stream-failures-dev` | Fallos de consumidores de Streams |
| 04 | SQS | `rtc-coverage-geospatial-stream-failures-dev` | Fallos de Streams/EventBridge de sincronización |
| 09 | SQS | `rtc-notifications-send-dev` | Intentos de envío → `send_notification` |
| 09 | SQS | `rtc-notifications-send-dlq-dev` | Envíos fallidos tras 5 recepciones |
| 09 | SQS | `rtc-notifications-stream-failures-dev` | Fallos de Streams/eventos internos |
| 09 | SNS | `rtc-notifications-delivery-dev` | Confirmaciones de SES → `process_delivery_status` |

SNS para SMS usa publicación directa al número autorizado; no requiere otro topic ni suscribir números al topic de confirmaciones. El stack también define el configuration set de SES y su destino SNS. La cuenta todavía necesita identidad SES verificada, acceso sandbox/producción apropiado y habilitación/cuotas de SMS. `DELIVERY_ENABLED=false` evita envíos externos; utiliza `IN_APP` en las pruebas. Oracle permanece deshabilitado por defecto y el análisis local funciona con Shapely/pyproj; su conexión real requiere Secrets Manager y conectividad TCPS a una base Oracle configurada.

## 4. Importar y configurar Postman

Importa estos dos archivos mediante **Import** y selecciona el environment `Red TeleCorp - AWS dev (completar)`:

- `postman/RedTeleCorp.postman_collection.json`
- `postman/RedTeleCorp.postman_environment.json`

Completa sus valores locales:

| Variable | Valor necesario |
|---|---|
| `telemetry_base_url` | `ServiceEndpoint` real de Telemetry, sin barra final |
| `coverage_base_url` | `ServiceEndpoint` real de Coverage, sin barra final |
| `notifications_base_url` | `ServiceEndpoint` real de Notifications, sin barra final |
| `cognito_id_token` | IdToken real de Cognito obtenido al iniciar sesión con Identity, sin escribir `Bearer ` delante |
| `antenna_id` | Antena existente en Infrastructure 02, preferentemente de desarrollo |
| `recipient_id` | Usuario activo en Identity 01 y propietario del token utilizado |
| `incident_id` | Identificador de prueba; por defecto `INC-POSTMAN-001` |

El IdToken debe pertenecer al User Pool de Identity. Para ejecutar toda la colección, usa un usuario del grupo `admin`; otros grupos tendrán los accesos indicados en la tabla anterior. Las consultas de notificaciones y preferencias siempre usan su `sub`, incluso para administradores. `recipient_id` debe corresponder al usuario del token para consultar luego la notificación propia. No hay una ruta de login inventada en la colección: se utiliza el inicio de sesión de Cognito del frontend/SDK de Identity.

Si cambias un grupo/rol en Identity, renueva la sesión o el IdToken antes de probar: el token ya emitido conserva los grupos que tenía al generarse.

Hay **18 requests**: 6 Telemetry, 7 Coverage y 5 Notifications. Cada uno incluye body/query de ejemplo y pruebas del código HTTP y contenido de respuesta. Los decimales DynamoDB pueden llegar como strings y los tests los convierten con `Number(...)` al comprobar rangos.

Ejecuta las carpetas en su orden con Collection Runner y **1500 ms de delay**. Los procesos SQS/Streams y GSIs son asíncronos; el delay reduce carreras, pero no garantiza su finalización. Si `latest` devuelve 404 después de 202 o el GSI aún no refleja una notificación, espera y repite la consulta. Una FeatureCollection vacía puede indicar que el índice aún se actualiza. No se considera ese 404 un éxito de la prueba.

La colección crea mediciones, una zona, un análisis, una notificación y **modifica cobertura y preferencias**. Utilízala en desarrollo. Crea una zona nueva por envío para evitar 409; los IDs se guardan en el environment. Conserva tus preferencias previas si necesitas restaurarlas. No hay un endpoint DELETE de zonas, por lo que las zonas de prueba permanecen guardadas. Notifications solicita exclusivamente `IN_APP`, con EMAIL/SMS desactivados en las preferencias de prueba.

`from` y `to` se inicializan a un intervalo de ±1 día si están vacíos. Si vuelves a probar días después, vacíalos o ajústalos. Los parámetros `cursor` están desactivados: cada respuesta guarda su siguiente cursor. Para avanzar, activa ese parámetro y conserva los mismos filtros/intervalo; vacíalo y desactívalo para volver a la primera página. Pon `limit=1` para probar varias páginas. Para probar idempotencia de notificaciones, usa `new_notification_each_run=false` y reenvía el mismo body con la misma `notification_idempotency_key`; un body distinto con esa clave debe dar 409.

El environment compartido deja el token vacío. Guárdalo solo como valor local en Postman y no exportes/commitees una copia con credenciales. `.gitignore` permite estos JSON de ejemplo y el Bash; mantiene ignorados `deploy_ec2.env`, salidas, paquetes y demás artefactos locales.

Opcionalmente, con Postman CLI instalado y un environment privado ya completado fuera de Git:

```bash
postman collection run postman/RedTeleCorp.postman_collection.json \
  -e /ruta/privada/redtelecorp-local.postman_environment.json \
  --delay-request 1500 --no-report-events
```

Las respuestas de notificaciones contienen datos del usuario; evita compartir logs del Runner. No se sube la colección a ningún workspace desde este repositorio.

## 5. Verificación realizada y límites

Verificación local del 14/11/2026: `bash -n` correcto; **14 escenarios Bash aprobados** con AWS/Serverless sustituidos (incluyen resolución del User Pool por SSM, ARN explícito de Identity en otra cuenta y rechazo de un ARN Lambda); **18/18 rutas coinciden** con los YAML y **37 scripts JavaScript** pasan la revisión de sintaxis. `serverless print` y `serverless package` terminaron correctamente en los tres servicios con el CLI local v3.40.0 y un ARN Cognito ficticio de validación. Framework v4, recomendado para EC2, no se ejecutó en esta validación local.

La simplificación anterior se validó con `serverless print`, `serverless package`, verificación de ZIP y `cfn-lint`. Posteriormente se cambió la autenticación de las 18 rutas a `COGNITO_USER_POOLS` y se retiró de Notifications el fragmento duplicado que invalidaba su YAML. Las pruebas de autenticación actuales están en `tests/test_cognito_authentication.py`; cubren los grupos de Identity, representaciones de claims, rechazo de scopes antiguos y propiedad de notificaciones. Estas son pruebas locales y no sustituyen una prueba con tokens emitidos por el User Pool desplegado. El CLI v3.40 tiene un catálogo antiguo de runtimes; usa Framework v4 en EC2 para Python 3.12.

La suite completa posterior a la migración terminó con **73 pruebas aprobadas y 2 omitidas**. Los ZIP contienen los 26 handlers actuales; el CloudFormation generado declara tres authorizers nativos y las 18 rutas con `AuthorizationType=COGNITO_USER_POOLS`, sin crear un User Pool nuevo. No se verificaron credenciales reales ni se desplegaron estos cambios.

Estas verificaciones no crean recursos y no certifican EC2, permisos IAM, autenticación, entrega de mensajes, Oracle ni endpoints reales. No se ejecutó la colección contra AWS. Se debe ejecutar el flujo contra el entorno AWS una vez configurados los valores anteriores.

Referencias: [Serverless deploy](https://www.serverless.com/framework/docs/providers/aws/cli-reference/deploy), [Serverless package](https://www.serverless.com/framework/docs/providers/aws/cli-reference/package), [importar en Postman](https://learning.postman.com/docs/getting-started/importing-and-exporting/importing-data), [Collection Runner](https://learning.postman.com/docs/tests-and-scripts/running-collections/intro-to-collection-runs/).
