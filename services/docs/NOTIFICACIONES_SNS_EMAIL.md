# Notificaciones por correo SNS e IN_APP

## Comportamiento
- Los eventos de detección, cambio de estado y resolución de incidentes generan una notificación por usuario asociado y por administrador activo del grupo Cognito `admin` con correo verificado.
- Los administradores reciben también una copia de las notificaciones manuales de POST /v1/notifications. Si ya eran el destinatario, se crea una sola notificación.
- Las copias de administración llevan admin_copy=true y no se omiten por preferencias personales; DELIVERY_ENABLED sigue controlando globalmente el envío externo.
- Usuarios normales conservan sus preferencias email_enabled e in_app_enabled.
- Canales admitidos: EMAIL e IN_APP. SMS se rechaza en nuevas solicitudes; mensajes SMS antiguos se marcan FAILED/CHANNEL_NOT_SUPPORTED.
- La API de preferencias conserva sms_enabled=false por compatibilidad y rechaza true.
- SNS envía correo de texto, no las antiguas plantillas HTML de SES. No hay recursos AWS::SES ni llamadas send_email.

## Aislamiento y suscripción
Cada usuario y dirección verificada tiene un topic determinista propio, con prefijo rtc-notifications-email-<stage> y un hash. Un cambio de dirección genera otro topic, sin publicar nuevos avisos al correo anterior. La aplicación comprueba que el topic no tenga otros destinatarios.
El primer intento crea el topic y la suscripción. SNS envía un correo de confirmación que debe aceptar cada destinatario, también los administradores. El aviso permanece BLOCKED/SNS_EMAIL_CONFIRMATION_REQUIRED hasta confirmar; se reintenta por SQS.
Tras agotar los reintentos, el mensaje va a la DLQ: después de confirmar hay que redirigirlo a la cola de envío. La confirmación no recupera automáticamente mensajes ya agotados.
SENT significa que SNS aceptó la publicación; no demuestra lectura ni entrega en el buzón. Los errores de proveedor ambiguos siguen siendo UNKNOWN para evitar duplicados.
Los topics personales se crean en tiempo de ejecución: no aparecen hasta el primer intento de envío. No se borran automáticamente cuando cambia un correo o se elimina un usuario.

## Despliegue
1. Copiar todos los cambios al checkout de la EC2, no solo el script.
2. Verificar los permisos existentes de LabRole con el administrador del laboratorio. El archivo LABROLE_INTEGRATION_POLICY.json es una referencia local; editarlo NO aplica permisos en AWS.
3. En deploy_all.env usar DELIVERY_ENABLED=true para correos reales, o false para probar solamente IN_APP. No se necesitan SES_IDENTITY, SES_FROM_ADDRESS ni números de celular.
4. Ejecutar desde services:

```bash
NODE_OPTIONS="--max-old-space-size=1024" bash deploy_all.sh check
NODE_OPTIONS="--max-old-space-size=1024" bash deploy_all.sh deploy
```

Además de DynamoDB/SQS y execute-api:Invoke hacia Identity/Reports, LabRole necesita:
- cognito-idp:ListUsersInGroup sobre el User Pool de Identity.
- sns:CreateTopic, sns:ListSubscriptionsByTopic, sns:Subscribe y sns:Publish sobre arn:aws:sns:us-east-1:573505212581:rtc-notifications-email-*.
COGNITO_USER_POOL_ARN se obtiene del mismo authorizer de Notifications; no se pide una lista manual de correos administradores. El grupo admin de Cognito es la fuente de los destinatarios administradores. Deben existir sus perfiles de Identity y correos verificados.
El despliegue usa el LabRole existente y no amplía sus permisos. El laboratorio podría limitar estas acciones; no se ha confirmado su autorización real.

## Pruebas y límites
Pruebas locales con Moto/SDK Stubber comprueban unión de destinatarios, idempotencia, paginación Cognito, exclusión de administradores desactivados, confirmación pendiente, copia obligatoria admin y rechazo de suscriptores ajenos. No son evidencia de correos reales.
Para probar en AWS: dos usuarios y un administrador, cada uno con un correo verificado; confirmar las suscripciones; generar incidentes distintos; verificar que el administrador recibe ambos y cada usuario solo el suyo. Luego repetir el mismo evento para comprobar la deduplicación.
Fuente AWS: https://docs.aws.amazon.com/sns/latest/api/API_Subscribe.html

## Evidencia local de esta revision
30 pruebas de ruteo/seguridad/autocontencion pasaron. La prueba completa entre microservicios no pudo completarse: el driver JS existente resuelve ../lib/projectMetric desde package.json y falla antes de llegar a Notifications. No se cambio ese driver ni se da el flujo completo por validado. El Postman CLI instalado no reconoce postman status; no se publico ni ejecuto una coleccion remota.
