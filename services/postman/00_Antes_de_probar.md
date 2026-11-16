# Probar cada endpoint de Red TeleCorp sin frontend

## Archivos para importar
1. Importar RedTeleCorp.all-endpoints.postman_collection.json en Postman.
2. Importar RedTeleCorp.all-endpoints.postman_environment.json y seleccionarlo en la esquina superior derecha.
3. Completar las URLs y el IdToken antes de enviar solicitudes.

Contiene 64 combinaciones metodo/ruta: 54 del flujo actual y 10 del servicio legado 07. Hay 53 rutas Cognito y 11 AWS_IAM. La coleccion coincide con las declaraciones HTTP de los ocho Serverless; excluye Lambdas de Streams, SQS y EventBridge que no exponen endpoint HTTP. No incluye las APIs de ejemplos de clase ni endpoints externos de GCP. No se ejecutaron solicitudes reales.

## Cargar automaticamente las URLs reales
Despues de desplegar, desde services en la EC2:

```bash
python3 postman/prepare_all_endpoints_environment.py
```

Importar el resultado .deploy-all-output/all-endpoints.environment.json. El script lee .deploy-all-output/postman.environment.json creado por deploy_all.sh y copia solo URLs e IDs Cognito. No copia JWT, claves AWS ni contrasenas.
El entorno principal del flujo usa reports_base_url para el servicio canonico 05; aqui se emplea incidents_base_url para 05 y legacy_reports_base_url exclusivamente para 07. Este ultimo se completa manualmente si 07 se desplego.
Si recibiste el ZIP fuera del repo, tambien puedes usar el script junto al JSON de plantilla y especificar:

```bash
python3 prepare_all_endpoints_environment.py --source /ruta/postman.environment.json --output /ruta/entorno-listo.json
```

## Autenticacion
- cognito_id_token: IdToken de un usuario administrador confirmado, sin el prefijo Bearer. Las solicitudes publicas heredan Bearer de la coleccion; el rol real determina permisos.
- user_id: valor sub del usuario autenticado. recipient_id apunta a user_id por defecto.
- Internos: autenticacion AWS Signature V4, region us-east-1 y servicio execute-api. Completar aws_access_key_id, aws_secret_access_key y aws_session_token con la sesion temporal vigente de AWS Academy. El IdToken no reemplaza estos valores. El rol debe permitir execute-api:Invoke en la ruta.
- Los campos sensibles se entregan vacios. Introducirlos solo localmente; type=secret oculta la interfaz, pero una exportacion puede incluir sus valores: no publicar ni subir a Git un entorno con secretos.

## Variables base_url
Completar identity_base_url, infrastructure_base_url, telemetry_base_url, coverage_base_url, incidents_base_url, field_base_url y notifications_base_url.
Copiar el endpoint exacto de CloudFormation, conservando /dev si forma parte de ServiceEndpoint. No agregar /v1: cada solicitud ya tiene su ruta. Evitar barra final. No usar la URL de la consola AWS ni FRONTEND_ORIGIN.

## Orden sugerido de pruebas
No ejecutar todo el catalogo como si fuera una transaccion: es una solicitud para cada ruta, no una garantia de flujo integrado automatico.
1. Identity: GET perfil con token valido. Las dos operaciones de administracion son opcionales.
2. Infrastructure: crear estacion y antena; despues probar listas, detalles y cambios sobre esos IDs.
3. Coverage: crear zona y guardar cobertura; despues consultar y analizar impacto. Esperar a que DynamoDB Streams actualice el indice antes de buscar por zona/bbox.
4. Telemetry: enviar medicion o lote; esperar SQS antes de consultar latest/history/summary/alerts.
5. Incidents: crear un reporte; se captura report_id. Para cambiar un incidente o consultar sus destinatarios, copiar un incident_id REAL de GET /incidencias?tipo=colectivos&zona=<zona>. Un reporte individual no garantiza la creacion inmediata de un incidente colectivo: hay umbrales y orquestacion.
6. Field: crear tecnico; crear orden con incidente/zona preparados. Se captura work_order_id. Asignar solo si esta PENDING_ASSIGNMENT; cambiar estado segun transiciones validas. Una orden creada puede quedar ya ASSIGNED.
7. Notifications: guardar preferencias, crear aviso IN_APP, listar, obtener y marcar como leido; se captura notification_id. Con DELIVERY_ENABLED=true en el backend pueden enviarse correos a los administradores incluso si la solicitud del usuario es IN_APP. Las suscripciones SNS requieren confirmacion.
8. Ejecutar internos con firma AWS una vez que existan los datos y permisos.

Los IDs de estacion, antena, zona y tecnico usan run_id para no pisar datos de otras pruebas. Se genera al primer envio. Para empezar otro conjunto de datos, vaciar run_id, report_id, work_order_id y notification_id. incident_id comienza con un ejemplo INC-PM-<run_id>: sustituirlo por el real para probar un incidente existente. El catalogo no acepta un 404/409 como prueba exitosa.
Las solicitudes de creacion capturan IDs devueltos en respuestas; si se produce un error, los siguientes detalles no tendran un ID valido.

## Opciones omitidas por defecto
- run_account_changes=true: habilita crear un usuario y cambiar su rol. Completar test_user_email, test_user_password y target_user_id. La contrasena inicial creada por Identity obliga a cambiarla en el primer inicio de sesion; no proporciona automaticamente un token.
- run_internal_writes=true: habilita las escrituras de integracion privada. Usarlas solo con datos de prueba.
- enable_ai=true: habilita POST soporte/chat; requiere que AI_ENABLED y la integracion GCP esten configurados. Con IA desactivada devuelve 503.
- enable_legacy=true: habilita 07 y requiere legacy_reports_base_url. No es necesario para el flujo canonico.
- require_oracle=true: exige VERIFIED_REQUEST en la prueba de impacto. Con false admite DISABLED/FALLBACK_LOCAL, pero esos estados no prueban integracion Oracle.

## Interpretar los resultados
- 200/201: consultar tambien el contenido; no demuestra que haya corrido una integracion externa.
- 202: solo aceptado/encolado. Esperar y consultar el recurso antes de afirmar que termino.
- 401: token ausente, vencido o incorrecto.
- 403: rol insuficiente o firma/permisos IAM incorrectos.
- 404: revisar base URL/stage e ID existente.
- 409: duplicado o transicion incompatible, no un resultado exitoso del caso positivo.
- oracle_status=VERIFIED_REQUEST: Oracle termino las consultas de esa solicitud; para demostrar interseccion usar geometria que se superponga y comprobar oracle_spatial_results.

## Verificacion de esta entrega
JSON validado contra esquema oficial Postman v2.1; inventario comparado con metodo/ruta de los ocho Serverless; scripts JavaScript compilados con Node. Los campos AWS/JWT/contrasena estan vacios. No se verifico conectividad ni comportamiento real en AWS.

## Registrarme e iniciar sesion sin frontend
Reimportar la coleccion y entorno actualizados. La carpeta 00 contiene 6 solicitudes adicionales a Cognito; el inventario del backend sigue siendo 64 rutas.
Completar aws_region=us-east-1 y cognito_client_id con el App Client ID del despliegue (no User Pool ID ni OCID).
En la EC2 puede consultarse sin imprimir claves:
```bash
aws ssm get-parameter --name /telecorp/dev/cognito/client-id --region us-east-1 --query Parameter.Value --output text
```
En Postman, completar auth_email con tu correo y auth_password con una clave propia de la aplicacion. No es la clave Oracle ni AWS. Minimo 8 caracteres con mayuscula, minuscula y numero, segun la plantilla Identity actual.
1. Si no tienes cuenta: 01 Registrarme; luego copiar en auth_confirmation_code el codigo que llega al correo y enviar 02 Confirmar correo.
2. Si tu correo aparece en ADMIN_EMAILS del despliegue, postConfirmation lo agrega al grupo admin despues de confirmar el correo. Los demas se agregan a clientes. Esto requiere que LabRole permita los triggers de Identity.
3. Ejecutar 03 Iniciar sesion. Si hay AuthenticationResult, se guardan cognito_id_token y user_id automaticamente. Luego probar GET /auth/perfil.
4. Si devuelve NEW_PASSWORD_REQUIRED: completar auth_new_password y ejecutar 05. Conservar manualmente la clave nueva en tu gestor y actualizar auth_password para futuros inicios de sesion. No se cambia ningun valor antes de que tu envies la solicitud.
5. 04 reenvia el codigo, 06 renueva el token. Otras respuestas de desafio (por ejemplo MFA) requieren su flujo especifico y no deben interpretarse como login completado.
Los valores de contrasenas, sesiones, codigos y tokens se entregan vacios. Se guardan solo cuando tu ejecutas las solicitudes; no exportar/sincronizar esos valores. No se han creado cuentas ni enviado correos desde esta tarea.
Referencias: https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_InitiateAuth.html y https://docs.aws.amazon.com/cognito-user-identity-pools/latest/APIReference/API_SignUp.html
