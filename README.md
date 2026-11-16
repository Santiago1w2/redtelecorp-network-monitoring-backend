# Red TeleCorp — Backend de monitoreo de red

Backend de Red TeleCorp para monitorear infraestructura de telecomunicaciones, registrar problemas de conectividad y coordinar su atención. Integra inventario de antenas, telemetría, cobertura geográfica, reportes de clientes, incidentes, órdenes de campo y notificaciones.

El proyecto utiliza microservicios en AWS, una integración geoespacial con Oracle y un servicio de inteligencia artificial en Google Cloud. El frontend se mantiene en un repositorio independiente y consume estas APIs.

## Funcionalidades principales

- Autenticación con Amazon Cognito y autorización por roles: `clientes`, `operador_soporte`, `supervisor_red`, `tecnico_campo` y `admin`.
- Consulta y gestión de estaciones y antenas, con coordenadas y datos de cobertura.
- Recepción de mediciones de antenas y generación de alertas de red.
- Registro de reportes individuales, detección de afectaciones colectivas, historial y métricas de incidentes.
- Análisis de intersección entre cobertura de antenas y zonas, incluyendo criticidad y área afectada.
- Coordinación de órdenes de trabajo para atención en campo.
- Notificaciones dentro de la aplicación y correo mediante SNS, según configuración, preferencias y suscripciones confirmadas.
- Asistencia mediante IA a través del backend; sus respuestas requieren revisión del equipo.
- Simulador opcional de telemetría y herramientas para cargar datos ficticios de demostración.

## Organización del repositorio

```text
redtelecorp-network-monitoring-backend/
├── README.md                         # Presentación general del proyecto
├── .gitignore
└── services/
    ├── network/                      # Red AWS: VPC, subredes, NAT y security group
    ├── identity-access/              # 01 · Identidad, usuarios y roles
    ├── network-infrastructure/       # 02 · Estaciones y antenas
    ├── network-telemetry/            # 03 · Mediciones, alertas y simulador
    ├── coverage-geospatial/          # 04 · Zonas, cobertura e impacto espacial
    ├── incidents-report/             # 05 · Reportes e incidentes del flujo actual
    ├── ia-network-intelligence/      # 06 · IA en Google Cloud Run
    ├── customer-reports/             # 07 · Servicio legado, despliegue opcional
    ├── field-operations/             # 08 · Órdenes de trabajo
    ├── notifications/                # 09 · Notificaciones y correo SNS
    ├── docs/                         # Guías de integración y demostración
    ├── postman/                      # Colecciones y entornos de pruebas
    ├── scripts/                      # Preparación de antenas y datos de evidencia
    ├── tests/                        # Pruebas de integración y despliegue
    ├── deploy_all.sh                 # Orquestador del despliegue AWS
    └── deploy_all.env.example        # Plantilla de configuración local
```

Las carpetas de ejemplos del curso que puedan existir en una copia local no forman parte del flujo principal de despliegue.

## Tecnologías y flujo de atención

| Componente | Uso en el proyecto |
| --- | --- |
| API Gateway y AWS Lambda | APIs HTTP y procesamiento de eventos; handlers en Node.js y Python |
| Amazon Cognito | Inicio de sesión, identidad y grupos de usuarios |
| Amazon DynamoDB | Persistencia operativa de inventario, mediciones, reportes, incidentes y otros registros |
| Amazon S3 | Artefactos del análisis geográfico |
| EventBridge, SQS y DynamoDB Streams | Comunicación asíncrona, colas y propagación de cambios |
| AWS Step Functions | Coordinación del flujo de análisis y atención de incidentes |
| Amazon SNS | Distribución de correo a suscriptores confirmados |
| Oracle Spatial | Consultas e intersecciones espaciales cuando la integración está habilitada |
| Google Cloud Run y Vertex AI | Servicio de asistencia y análisis mediante IA |
| Serverless Framework y CloudFormation | Definición y despliegue de infraestructura AWS |

Un cliente registra un reporte autenticado. El servicio de incidentes lo persiste y lo relaciona con el contexto de la zona, las alertas y otros reportes. El flujo puede consultar cobertura, telemetría e IA, solicitar una orden de campo y emitir eventos para notificar los cambios de atención.

**Oracle y persistencia:** la base Oracle debe estar configurada con los datos espaciales necesarios para las consultas. En el flujo actual, la persistencia operativa de la aplicación se realiza principalmente en DynamoDB y los artefactos geográficos se guardan en S3. Activar Oracle no traslada automáticamente los usuarios, reportes o incidentes a esa base de datos.

## Preparar la configuración

El despliegue conjunto se ejecuta con **Bash**, por ejemplo desde una EC2 Linux. Requiere AWS CLI, Node.js/npm, Serverless Framework, Python y `uv`, además de credenciales AWS vigentes y los permisos del laboratorio.

La configuración actual del script está vinculada a la cuenta AWS `573505212581`, la región `us-east-1` y un `LabRole` existente. Para reutilizar el proyecto en otra cuenta hay que adaptar esas referencias y validar los permisos. El script no modifica los permisos de `LabRole`.

Desde la raíz del repositorio, en Bash:

```bash
cd services
cp deploy_all.env.example deploy_all.env
```

Completa el archivo local antes de ejecutar el despliegue. La plantilla contiene valores del entorno de desarrollo y opciones activadas que requieren recursos externos; copiarla no significa que esté lista para otro entorno.

| Variable | Configuración necesaria |
| --- | --- |
| `FRONTEND_ORIGIN` | Origen del frontend para CORS, sin ruta ni barra final. El validador actual exige HTTPS. |
| `ADMIN_EMAILS` | Correos administradores separados por coma. |
| `NETWORK_MODE` | `existing` reutiliza IDs de red; `create` crea una red administrada por el stack. |
| `ORACLE_ENABLED` | Activa Oracle; requiere `ORACLE_SECRET_ARN`, conectividad y ACL en OCI. |
| `AI_ENABLED` | Activa la integración IA; requiere URL y autenticación del servicio GCP. |
| `GCP_AUTH_MODE` | `WIF` o `API_KEY`. WIF requiere audiencia y cuenta de servicio invocadora. |
| `DELIVERY_ENABLED` | Habilita entrega externa; el correo depende de suscripciones SNS confirmadas y permisos. |
| `SIMULATOR_ENABLED` | Activa el simulador en `dev`; requiere los 20 IDs preparados. |
| `DEPLOY_LEGACY_REPORTS` | Mantener `false` para el flujo actual: `incidents-report` es el propietario canónico de reportes. |

En modo `existing` se necesitan `NETWORK_VPC_ID`, `NETWORK_EGRESS_IP`, `ORACLE_SECURITY_GROUP_IDS` y `ORACLE_SUBNET_IDS`. En modo `create`, el script obtiene los IDs e IP del stack de red. La creación de NAT e IPv4 pública genera cargos. Consulta la [guía de red](services/network/README.md) antes de cambiar el modo de una red existente.

Las contraseñas y claves de integraciones se administran con Secrets Manager o SSM SecureString según corresponda. Mantén las credenciales, JWT y archivos de entorno completados fuera de Git.

## Desplegar AWS

Desde `services/`, en Bash:

```bash
# Resolver configuración y comprobar prerrequisitos; no crea stacks.
bash deploy_all.sh check

# Generar paquetes de comprobación.
bash deploy_all.sh package

# Crear o actualizar la infraestructura y las APIs.
bash deploy_all.sh deploy
```

El orquestador despliega primero la red y después Identity, Telemetry, Infrastructure, Field Operations, Coverage, Incidents y Notifications. Repite Telemetry tras obtener la URL de Infrastructure para completar su integración. El servicio legado 07 se incluye únicamente si se habilita expresamente.

Las salidas se guardan en `services/.deploy-all-output/`, incluidos los datos de red y un entorno Postman con las URLs reales. Los paquetes de comprobación usan referencias locales de validación; la acción `deploy` vuelve a construir con las salidas reales.

Este comando despliega los servicios AWS. La base Oracle, su ACL, el servicio GCP, sus secretos y la federación WIF requieren configuración independiente. Un despliegue satisfactorio no demuestra por sí solo que esas conexiones funcionen.

## Probar las APIs sin frontend

1. Importa en Postman la [colección de endpoints](services/postman/RedTeleCorp.all-endpoints.postman_collection.json) y el [entorno de ejemplo](services/postman/RedTeleCorp.all-endpoints.postman_environment.json).
2. Después del despliegue, genera el entorno con las URLs reales desde `services/`:

   ```bash
   python3 postman/prepare_all_endpoints_environment.py
   ```

3. Importa `services/.deploy-all-output/all-endpoints.environment.json` y selecciona ese entorno.
4. Usa las solicitudes de Cognito para registrarte o iniciar sesión. Las solicitudes del usuario utilizan su token; las rutas internas con `AWS_IAM` requieren firma AWS Signature V4 y credenciales temporales autorizadas.

Conserva la URL exacta de cada API: algunas incluyen `/dev` y otras no. La colección incluye rutas del servicio legado, que no estarán disponibles si no se desplegó. Las operaciones de escritura pueden crear datos y generar notificaciones; utiliza cuentas y datos de prueba.

Consulta los [pasos de autenticación y pruebas](services/postman/00_Antes_de_probar.md) para completar las variables y entender los permisos de cada solicitud.

## Documentación del equipo

- [Red AWS y conectividad para Oracle](services/network/README.md).
- [Identidad y acceso](services/identity-access/README.md).
- [Reportes, incidentes y orquestación](services/incidents-report/README.md).
- [Servicio de IA en Google Cloud](services/ia-network-intelligence/Readme.md).
- [Notificaciones por correo mediante SNS](services/docs/NOTIFICACIONES_SNS_EMAIL.md).
- [Simulador de antenas](services/docs/ANTENNA_SIMULATOR.md).
- [Datos ficticios de evidencia en Lima](services/docs/EVIDENCIA_LIMA.md).
- [Colección completa de Postman y autenticación](services/postman/00_Antes_de_probar.md).

Este repositorio se utiliza en un entorno de desarrollo y demostración. La disponibilidad de las integraciones depende de las credenciales, permisos y recursos activos de cada proveedor.
