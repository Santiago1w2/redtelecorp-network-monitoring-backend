# Red AWS antes de las APIs

`rtc-network-${stage}` es un servicio Serverless sin endpoints ni Lambdas.
`services/deploy_all.sh` lo despliega antes de Identity y de las demas APIs,
lee sus Outputs y pasa los IDs reales a Coverage. Los otros servicios conservan
su red actual; solo las funciones de impacto que necesitan Oracle se conectan
a las subredes privadas. No se configura nada en OCI ni se leen contrasenas.

## Red actual de pruebas

Conservar este bloque en el **deploy_all.env existente en EC2**, ademas de las
demas opciones del despliegue. Actualizar el archivo de ejemplo en Git no cambia
ese archivo local automaticamente.

```bash
NETWORK_MODE=existing
NETWORK_VPC_ID='vpc-0bbf2ecd85925f839'
NETWORK_EGRESS_IP='44.199.117.193'
ORACLE_SECURITY_GROUP_IDS='sg-073441fce8eae8dff'
ORACLE_SUBNET_IDS='subnet-00d8e8be43d3c8dd3,subnet-058b1550e4f51165c'
ORACLE_TLS_PORT=1521
```

Este modo referencia la red creada previamente: **no importa su propiedad a
CloudFormation, no la modifica y no crea otro NAT**. El stack conserva el modo
en SSM y publica IDs/IP como Outputs y exports. Los recursos siguen administrados
fuera del stack. Esta es la opcion del ejemplo para la cuenta actual.

## Crear toda la red automaticamente en otro entorno

```bash
NETWORK_MODE=create
ORACLE_TLS_PORT=1521
```

El stack crea una VPC `10.42.0.0/16` con DNS, una subred publica y dos privadas
en AZ diferentes, Internet Gateway, NAT zonal con Elastic IP, tablas de rutas,
endpoint Gateway S3 y un security group sin entrada. La salida permite TCP 443
y el puerto TLS indicado. Un NAT compartido reduce el costo de dev y no ofrece
alta disponibilidad entre AZ. NAT, IPv4 publica y trafico generan cargos.

No hacen falta IDs de VPC/subredes/SG/IP en modo create. Los IDs locales usados
por `check` y `package` son solo para validacion; `deploy` los reemplaza por
salidas reales antes de desplegar Coverage. Si falla la red o faltan salidas,
el script termina sin desplegar las APIs.

El modo se conserva en el Output `NetworkMode`. El script rechaza cambiar
`existing` por `create`, o viceversa, en un stack ya existente. Una migracion
requiere planificar propiedad y dependencias; no se importan ni borran recursos
automaticamente. No ejecutar `serverless remove` sobre una red administrada en uso.

## Ejecutar desde services

```bash
bash deploy_all.sh check
bash deploy_all.sh deploy
```

El script guarda `.deploy-all-output/network.json` y `network.env` y muestra la
IP de salida que debe autorizarse en Oracle. `ORACLE_ENABLED` controla el uso
de Oracle por Coverage; la red se despliega primero en ambos estados.
`ORACLE_SECRET_ARN` sigue siendo necesario para activar Oracle.

AWS debe permitir a la identidad de despliegue crear recursos EC2/VPC y el
parametro SSM, ademas de CloudFormation/S3 usados por Serverless. `LabRole`
debe poder gestionar interfaces de Lambda y leer el secreto existente; este
servicio no modifica sus permisos. El script antiguo `deploy_ec2.sh` no ejecuta
este servicio: utilizar `deploy_all.sh` para este flujo.

La creacion de una red nueva puede cambiar la IP que Oracle tiene autorizada.
La ACL de OCI, el usuario SQL, el secreto y la prueba Spatial siguen siendo
pasos separados. Desplegar la red no demuestra una conexion SQL satisfactoria.

## Verificacion local

`tests/test_network_deployment.py` ejecuta el orquestador con dobles de CLI:
comprueba ambos modos, orden de despliegue, propagacion de IDs, detencion ante
fallos, bloqueo de cambio de modo y ausencia de despliegues en `check`.
Estas pruebas no crean recursos AWS ni prueban conectividad real.
