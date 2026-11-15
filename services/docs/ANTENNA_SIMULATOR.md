# Simulador de 20 antenas en dev

La Lambda `rtc-network-telemetry-simulate-antennas-dev` pertenece al servicio
Telemetry. Una regla EventBridge `rate(1 minute)` la invoca cada minuto cuando
`SIMULATOR_ENABLED=true`. La programacion queda deshabilitada por defecto.

## Flujo y receptor

Para una antena que envia HTTP, el receptor sigue siendo `postTelemetry` en
`POST /v1/telemetry` o `postTelemetryBatch` en `POST /v1/telemetry/batch`, con
autenticacion Cognito. Estas funciones validan y encolan la medicion.

El simulador de AWS usa `LabRole`: comprueba las antenas mediante la API interna
de Infrastructure con firma IAM y publica directamente en la misma cola SQS.
Por tanto, no llama a la ruta publica ni prueba el ingreso HTTP/Cognito.
`processTelemetry` recibe los mensajes de SQS, persiste el historial y ultimo
estado en DynamoDB y genera los cambios de estado que alimentan el flujo.
El simulador no decide el estado final ni escribe directamente en esas tablas.

## Datos generados

Se registran una estacion de pruebas y 20 antenas mediante la API real de
Infrastructure. Los IDs `ANT-SIM-<uuid>` son aleatorios una sola vez y se guardan
en `.antenna-simulator/manifest.json`. Reejecutar el preparador con ese manifest
reutiliza los mismos recursos; no se cambian los IDs cada minuto.

Cada ejecucion selecciona 5 IDs distintos al azar de ese conjunto. Configurar
`SIMULATOR_SAMPLE_SIZE=20` envia una medicion de cada antena en orden aleatorio;
se admite cualquier tamano entre 1 y 20. Todas pertenecen a la estacion y zona
elegidas al preparar la simulacion.

| Estado esperado por el procesador | traffic_gb_min | Probabilidad por medicion |
|---|---|---|
| DISABLED | 0 | 5 % |
| DOWN | Entero entre 1 y 59 | 15 % |
| NORMAL | Entero entre 60 y 129 | 70 % |
| SATURATED | Entero entre 130 y 180 | 10 % |

Estas probabilidades no garantizan una cantidad exacta de estados por minuto.
Se envia timestamp UTC. La misma entrega EventBridge (id/time) genera el mismo
lote en sus reintentos; el consumidor existente deduplica las mediciones. Los
errores parciales de SQS se propagan para reintento, no se declaran exitosos.

## Preparar las antenas una vez

Desde `services` en EC2, con las APIs existentes desplegadas, escoger una zona
de pruebas existente y reemplazar `ID_ZONA_EXISTENTE` en este comando:

```bash
python3 scripts/prepare_antenna_simulator.py \
  --infrastructure-url 'https://vxj8wme5da.execute-api.us-east-1.amazonaws.com' \
  --zone-id 'ID_ZONA_EXISTENTE' \
  --latitude -12.105 --longitude -77.035 \
  --client-id "$(aws ssm get-parameter --name /telecorp/dev/cognito/client-id --region us-east-1 --query Parameter.Value --output text)"
```

Pide usuario y contrasena Cognito de administrador por terminal; la contrasena
se introduce de forma oculta. No se guardan credenciales en el manifest, en
Lambda ni en Git. Tambien admite un IdToken existente mediante la variable
local `COGNITO_ID_TOKEN`. Este preparador usa solo la biblioteca estandar Python.

Si hay una interrupcion, repetir el mismo comando con el mismo manifest.
Si recibe un desafio MFA/cambio de contrasena, completarlo por separado y usar
un IdToken valido. El preparador no modifica cuentas ni sus contrasenas.

Al terminar muestra una linea `source '.../.antenna-simulator/simulator.env'`.
Agregar esa linea **al final del deploy_all.env de EC2**. El archivo generado
contiene los 20 IDs, `SIMULATOR_ENABLED=true` y `SIMULATOR_SAMPLE_SIZE=5`.
Es una configuracion local que no se versiona; conservar ambos archivos.

## Desplegar y comprobar

```bash
export NODE_OPTIONS="--max-old-space-size=1024"
bash deploy_all.sh check
bash deploy_all.sh deploy
```

El primer pase de Telemetry mantiene la regla apagada hasta obtener la URL real
de Infrastructure; el segundo pase aplica la opcion configurada. El script
comprueba que hay exactamente 20 IDs demo distintos y restringe la simulacion a
stage dev. `LabRole` necesita `execute-api:Invoke` sobre la consulta interna de
Infrastructure y `sqs:SendMessage` sobre la cola; ya son permisos utilizados por
la ingesta de Telemetry. Este cambio no modifica politicas IAM.

Despues de esperar al menos un minuto y el procesamiento de la cola, consultar
en Postman con IdToken Cognito de staff:

```http
GET https://6s5vvqxpg2.execute-api.us-east-1.amazonaws.com/dev/v1/telemetry/antennas/ANT-SIM-ID_DEL_MANIFEST/latest
GET https://6s5vvqxpg2.execute-api.us-east-1.amazonaws.com/dev/v1/telemetry/alerts
Authorization: Bearer ID_TOKEN
```

Con muestra de 5, una antena concreta puede no ser seleccionada en el primer
minuto. Para observar las 20 en cada ciclo, establecer el tamano en 20 antes
del despliegue. Un resultado de Lambda con `submitted` confirma aceptacion SQS;
la lectura posterior confirma persistencia.

## Detener

Cambiar `SIMULATOR_ENABLED=false` en `.antenna-simulator/simulator.env` (o poner
esa asignacion despues de su source en deploy_all.env) y redesplegar. Esto
deshabilita la regla y conserva los datos de demostracion. Las mediciones pueden
crear alertas, incidentes y notificaciones reales del entorno dev y consumen
recursos mientras la regla este habilitada.

Las pruebas locales usan SQS/DynamoDB simulados y el consumidor real. No se
afirma haber desplegado ni activado la regla en AWS durante la implementacion.
