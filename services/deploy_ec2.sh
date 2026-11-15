#!/usr/bin/env bash
# Ejecutar desde una EC2 Linux. No instala herramientas ni imprime credenciales.
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ACTION="check"
TARGET="all"
ENV_FILE="$ROOT_DIR/deploy_ec2.env"

usage() {
  cat <<'HELP'
Uso: bash deploy_ec2.sh [check|package|deploy] [all|telemetry|coverage|notifications] [--env ARCHIVO]

  check    Comprueba herramientas, configuración, credenciales y bus; no despliega.
  package  Construye los paquetes y CloudFormation; no crea stacks.
  deploy   Despliega/actualiza los servicios elegidos mediante Serverless.

Ejemplos:
  cp deploy_ec2.env.example deploy_ec2.env
  bash deploy_ec2.sh check all
  bash deploy_ec2.sh package coverage
  bash deploy_ec2.sh deploy all
  bash deploy_ec2.sh deploy notifications --env /ruta/config.env

Lee EC2_Y_POSTMAN.md. El modo deploy crea recursos facturables en AWS.
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
warn() { printf 'AVISO: %s\n' "$*" >&2; }
need() { command -v "$1" >/dev/null 2>&1 || fail "Falta $1. Consulta EC2_Y_POSTMAN.md."; }

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then usage; exit 0; fi
if [[ $# -gt 0 && "$1" != "--env" ]]; then ACTION="$1"; shift; fi
if [[ $# -gt 0 && "$1" != "--env" ]]; then TARGET="$1"; shift; fi
while [[ $# -gt 0 ]]; do
  case "$1" in
    --env) [[ $# -ge 2 ]] || fail 'Falta el archivo después de --env.'; ENV_FILE="$2"; shift 2 ;;
    *) fail "Argumento desconocido: $1" ;;
  esac
done
case "$ACTION" in check|package|deploy) ;; *) usage; fail 'Acción inválida.' ;; esac
case "$TARGET" in all|telemetry|coverage|notifications) ;; *) usage; fail 'Servicio inválido.' ;; esac

[[ -f "$ENV_FILE" ]] || fail "Copia deploy_ec2.env.example a $ENV_FILE y completa sus valores."
# Es un archivo Bash local de confianza; no usar archivos descargados de terceros.
set -a
source "$ENV_FILE"
set +a

STAGE="${STAGE:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
EVENT_BUS_NAME="${EVENT_BUS_NAME:-rtc-events-$STAGE}"
CREATE_EVENT_BUS="${CREATE_EVENT_BUS:-auto}"
SERVERLESS_BIN="${SERVERLESS_BIN:-serverless}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
DELIVERY_ENABLED="${DELIVERY_ENABLED:-false}"
ORACLE_ENABLED="${ORACLE_ENABLED:-false}"
MAX_BATCH="${MAX_BATCH:-100}"
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER="" AWS_CLI_AUTO_PROMPT=off

[[ "$STAGE" =~ ^[a-zA-Z0-9-]+$ ]] || fail 'STAGE solo admite letras, números y guiones.'
[[ "$AWS_REGION" =~ ^[a-z0-9-]+$ ]] || fail 'AWS_REGION inválida.'
[[ "${FRONTEND_ORIGIN:-}" =~ ^https://[^/?\#[:space:]]+$ ]] || fail 'FRONTEND_ORIGIN debe ser el origen HTTPS sin ruta ni barra final.'
[[ "${IDENTITY_AUTHORIZER_ARN:-}" =~ ^arn:aws[a-z-]*:lambda:${AWS_REGION}:[0-9]{12}:function:.+ ]] || fail 'Configura IDENTITY_AUTHORIZER_ARN con la Lambda authorizer real de esta región.'
case "$CREATE_EVENT_BUS" in auto|true|false) ;; *) fail 'CREATE_EVENT_BUS debe ser auto, true o false.' ;; esac
case "$DELIVERY_ENABLED:$ORACLE_ENABLED" in true:true|true:false|false:true|false:false) ;; *) fail 'DELIVERY_ENABLED y ORACLE_ENABLED deben ser true o false.' ;; esac
[[ "$MAX_BATCH" =~ ^[0-9]+$ ]] && (( 10#$MAX_BATCH >= 1 && 10#$MAX_BATCH <= 500 )) || fail 'MAX_BATCH debe estar entre 1 y 500.'

need aws
need node
need "$SERVERLESS_BIN"
need "$PYTHON_BIN"
"$PYTHON_BIN" -c 'import sys; assert sys.version_info >= (3, 10), "Se necesita Python 3.10 o posterior para construir los paquetes"'
if [[ "$TARGET" == all || "$TARGET" == coverage ]]; then need uv; fi
AWS=(aws --region "$AWS_REGION")
[[ -z "${AWS_PROFILE:-}" ]] || AWS+=(--profile "$AWS_PROFILE")
AWS_ERROR_FILE="$(mktemp)"
trap 'rm -f -- "$AWS_ERROR_FILE"' EXIT
ACCOUNT_ID="$("${AWS[@]}" sts get-caller-identity --query Account --output text)" || fail 'No se pudo autenticar con AWS. Revisa el instance profile o AWS_PROFILE.'
[[ "$ACCOUNT_ID" =~ ^[0-9]{12}$ ]] || fail 'AWS no devolvió un identificador de cuenta válido.'
LAMBDA_ROLE_ARN="${LAMBDA_ROLE_ARN:-arn:aws:iam::$ACCOUNT_ID:role/LabRole}"
[[ "$LAMBDA_ROLE_ARN" == "arn:aws:iam::$ACCOUNT_ID:role/"* ]] || fail 'LAMBDA_ROLE_ARN debe pertenecer a la cuenta autenticada.'
[[ "$IDENTITY_AUTHORIZER_ARN" == "arn:aws:lambda:$AWS_REGION:$ACCOUNT_ID:function:"* ]] || fail 'El authorizer debe pertenecer a esta cuenta/región.'
printf 'Cuenta: %s | Región: %s | Stage: %s | Acción: %s\n' "$ACCOUNT_ID" "$AWS_REGION" "$STAGE" "$ACTION"
warn 'LabRole es un rol existente: el stack no cambia sus permisos. Consulta EC2_Y_POSTMAN.md.'

# Conserva el parámetro del stack propietario para no eliminar el bus en una actualización.
TELEMETRY_STACK="rtc-network-telemetry-$STAGE"
PREVIOUS_CREATE="false"
if PREVIOUS_CREATE="$("${AWS[@]}" cloudformation describe-stacks --stack-name "$TELEMETRY_STACK" --query 'Stacks[0].Parameters[?ParameterKey==`CreateEventBus`].ParameterValue | [0]' --output text 2>"$AWS_ERROR_FILE")"; then
  [[ "$PREVIOUS_CREATE" == true ]] || PREVIOUS_CREATE=false
elif [[ "$(<"$AWS_ERROR_FILE")" == *'does not exist'* ]]; then
  PREVIOUS_CREATE=false
else
  cat "$AWS_ERROR_FILE" >&2
  fail 'No se pudo consultar el stack de Telemetry; no se asumirá que está ausente.'
fi
BUS_EXISTS=false
if "${AWS[@]}" events describe-event-bus --name "$EVENT_BUS_NAME" >/dev/null 2>"$AWS_ERROR_FILE"; then
  BUS_EXISTS=true
elif [[ "$(<"$AWS_ERROR_FILE")" != *ResourceNotFoundException* ]]; then
  cat "$AWS_ERROR_FILE" >&2
  fail 'No se pudo comprobar EventBridge.'
fi
if [[ "$CREATE_EVENT_BUS" == auto ]]; then
  if [[ "$PREVIOUS_CREATE" == true ]]; then CREATE_EVENT_BUS=true
  elif [[ "$BUS_EXISTS" == false && ( "$TARGET" == all || "$TARGET" == telemetry ) ]]; then CREATE_EVENT_BUS=true
  else CREATE_EVENT_BUS=false
  fi
fi
[[ "$PREVIOUS_CREATE:$CREATE_EVENT_BUS" != true:false ]] || fail 'El stack de Telemetry es propietario del bus. Conserva CREATE_EVENT_BUS=true o auto.'
[[ "$BUS_EXISTS:$CREATE_EVENT_BUS:$PREVIOUS_CREATE" != true:true:false ]] || fail 'El bus ya existe fuera del stack de Telemetry. Usa CREATE_EVENT_BUS=false o auto.'
if [[ "$BUS_EXISTS" == false && ( "$CREATE_EVENT_BUS" != true || ( "$TARGET" != all && "$TARGET" != telemetry ) ) ]]; then
  fail 'Falta el bus compartido. Despliega primero telemetry (o all) con CREATE_EVENT_BUS=auto.'
fi
printf 'Bus compartido: %s | existe=%s | CreateEventBus=%s\n' "$EVENT_BUS_NAME" "$BUS_EXISTS" "$CREATE_EVENT_BUS"

[[ -n "${INFRASTRUCTURE_API_URL:-}" ]] || warn 'INFRASTRUCTURE_API_URL vacía: ingesta de telemetría y PUT de cobertura no podrán validar antenas.'
if [[ "$TARGET" == all || "$TARGET" == notifications ]]; then
  [[ -n "${IDENTITY_API_URL:-}" ]] || warn 'IDENTITY_API_URL vacía: no se podrán validar destinatarios.'
  [[ -n "${REPORTS_API_URL:-}" ]] || warn 'REPORTS_API_URL vacía: los eventos de incidentes no podrán resolver destinatarios.'
  [[ "$DELIVERY_ENABLED" == true ]] || warn 'Envío externo deshabilitado. Prueba las notificaciones con channels=["IN_APP"].'
  [[ "$DELIVERY_ENABLED" != true || ( -n "${SES_FROM_ADDRESS:-}" && -n "${SES_IDENTITY:-}" ) ]] || fail 'Para DELIVERY_ENABLED=true configura SES_FROM_ADDRESS y SES_IDENTITY.'
fi
[[ "$ORACLE_ENABLED" != true || -n "${ORACLE_SECRET_ARN:-}" ]] || fail 'Para ORACLE_ENABLED=true configura ORACLE_SECRET_ARN.'

COMMON_PARAMS=(--stage "$STAGE" --region "$AWS_REGION"
  --param="frontendOrigin=$FRONTEND_ORIGIN"
  --param="identityAuthorizerArn=$IDENTITY_AUTHORIZER_ARN"
  --param="lambdaRoleArn=$LAMBDA_ROLE_ARN"
  --param="eventBusName=$EVENT_BUS_NAME")

run_service() (
  local selection="$1" directory="" service="" parameters=()
  case "$selection" in
    telemetry)
      directory=network-telemetry; service=rtc-network-telemetry
      parameters+=(--param="maxBatch=$MAX_BATCH" --param="createEventBus=$CREATE_EVENT_BUS")
      [[ -z "${INFRASTRUCTURE_API_URL:-}" ]] || parameters+=(--param="infrastructureApiUrl=$INFRASTRUCTURE_API_URL")
      [[ -z "${INFRASTRUCTURE_API_ARN:-}" ]] || parameters+=(--param="integrationApiArn=$INFRASTRUCTURE_API_ARN") ;;
    coverage)
      directory=coverage-geospatial; service=rtc-coverage-geospatial
      parameters+=(--param="oracleEnabled=$ORACLE_ENABLED")
      [[ -z "${INFRASTRUCTURE_API_URL:-}" ]] || parameters+=(--param="infrastructureApiUrl=$INFRASTRUCTURE_API_URL")
      [[ -z "${ORACLE_SECRET_ARN:-}" ]] || parameters+=(--param="oracleSecretArn=$ORACLE_SECRET_ARN")
      [[ -z "${INFRASTRUCTURE_API_ARN:-}" ]] || parameters+=(--param="integrationApiArn=$INFRASTRUCTURE_API_ARN") ;;
    notifications)
      directory=notifications; service=rtc-notifications
      parameters+=(--param="deliveryEnabled=$DELIVERY_ENABLED" --param="sesIdentity=${SES_IDENTITY:-unconfigured.invalid}")
      [[ -z "${IDENTITY_API_URL:-}" ]] || parameters+=(--param="identityApiUrl=$IDENTITY_API_URL")
      [[ -z "${REPORTS_API_URL:-}" ]] || parameters+=(--param="reportsApiUrl=$REPORTS_API_URL")
      [[ -z "${SES_FROM_ADDRESS:-}" ]] || parameters+=(--param="sesFromAddress=$SES_FROM_ADDRESS")
      [[ -z "${IDENTITY_API_ARN:-}" ]] || parameters+=(--param="integrationApiArn=$IDENTITY_API_ARN")
      [[ -z "${REPORTS_API_ARN:-}" ]] || parameters+=(--param="reportsApiArn=$REPORTS_API_ARN") ;;
  esac
  cd -- "$ROOT_DIR/$directory"
  printf '\n%s: %s\n' "$ACTION" "$directory"
  "$SERVERLESS_BIN" print "${COMMON_PARAMS[@]}" "${parameters[@]}" >/dev/null
  if [[ "$ACTION" == check ]]; then
    printf 'Configuración Serverless resuelta. No se han creado recursos.\n'
    exit 0
  fi
  if [[ "$selection" == coverage ]]; then
    "$PYTHON_BIN" handlers/post_impact_analysis.py --build-package
  fi
  "$SERVERLESS_BIN" "$ACTION" "${COMMON_PARAMS[@]}" "${parameters[@]}"
  if [[ "$ACTION" == deploy ]]; then
    # Salidas reales del stack; la API incluye el stage y sirve como base_url en Postman.
    mkdir -p -- "$ROOT_DIR/.deploy-output"
    local output_file="$ROOT_DIR/.deploy-output/$selection-$STAGE.json"
    "${AWS[@]}" cloudformation describe-stacks --stack-name "$service-$STAGE" --query 'Stacks[0].Outputs' --output json >"$output_file"
    printf 'Salidas AWS guardadas en %s\n' "$output_file"
    "${AWS[@]}" cloudformation describe-stacks --stack-name "$service-$STAGE" --query 'Stacks[0].Outputs[?OutputKey==`ServiceEndpoint`].OutputValue' --output text
  fi
)

if [[ "$TARGET" == all ]]; then
  # Telemetry crea el bus cuando aún no existe; los demás lo utilizan.
  for selection in telemetry coverage notifications; do run_service "$selection"; done
else
  run_service "$TARGET"
fi
printf '\nFinalizado: %s %s.\n' "$ACTION" "$TARGET"
