#!/usr/bin/env bash
# Despliega los ocho Serverless AWS desde EC2. Google Cloud y Oracle se configuran aparte.
set -euo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ACTION="${1:-check}"
ENV_FILE="${2:-$ROOT_DIR/deploy_all.env}"
case "$ACTION" in check|package|deploy) ;; *) printf 'Uso: bash deploy_all.sh [check|package|deploy] [archivo.env]\n' >&2; exit 2 ;; esac
fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || fail "Falta instalar $1."; }
[[ -f "$ENV_FILE" ]] || fail 'Copia deploy_all.env.example a deploy_all.env y completa frontend y correo administrador.'
set -a
source "$ENV_FILE"
set +a
STAGE="${STAGE:-dev}"
AWS_REGION="${AWS_REGION:-us-east-1}"
SERVERLESS_BIN="${SERVERLESS_BIN:-serverless}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
AI_ENABLED="${AI_ENABLED:-false}"
ORACLE_ENABLED="${ORACLE_ENABLED:-false}"
DELIVERY_ENABLED="${DELIVERY_ENABLED:-false}"
EVENT_BUS_NAME="${EVENT_BUS_NAME:-rtc-events-$STAGE}"
CREATE_EVENT_BUS="${CREATE_EVENT_BUS:-auto}"
[[ "$STAGE" =~ ^[a-zA-Z0-9-]+$ ]] || fail 'STAGE invalido.'
[[ "$AWS_REGION" == us-east-1 ]] || fail 'La configuración inicial está preparada para us-east-1; usa esa región en todos los servicios.'
[[ "${FRONTEND_ORIGIN:-}" =~ ^https://[^/?\#[:space:]]+$ && "$FRONTEND_ORIGIN" != *REEMPLAZAR* ]] || fail 'Configura FRONTEND_ORIGIN con el origen HTTPS real.'
[[ -n "${ADMIN_EMAILS:-}" && "$ADMIN_EMAILS" == *@* && "$ADMIN_EMAILS" != *REEMPLAZAR* ]] || fail 'Configura ADMIN_EMAILS con los correos administradores separados por coma.'
for flag in "$AI_ENABLED" "$ORACLE_ENABLED" "$DELIVERY_ENABLED"; do
  case "$flag" in true|false) ;; *) fail 'Los indicadores de integración admiten true/false.' ;; esac
done
[[ "$AI_ENABLED" != true || ( -n "${GCP_AI_BASE_URL:-}" && -n "${GCP_AI_SECRET_ARN:-}" ) ]] || fail 'IA requiere GCP_AI_BASE_URL y GCP_AI_SECRET_ARN.'
[[ "$ORACLE_ENABLED" != true || -n "${ORACLE_SECRET_ARN:-}" ]] || fail 'Oracle requiere ORACLE_SECRET_ARN y red configurada.'
[[ "$DELIVERY_ENABLED" != true || ( -n "${SES_FROM_ADDRESS:-}" && -n "${SES_IDENTITY:-}" ) ]] || fail 'La entrega externa requiere identidad SES verificada.'
for tool in aws node npm "$SERVERLESS_BIN" "$PYTHON_BIN" uv; do need "$tool"; done
export AWS_REGION AWS_DEFAULT_REGION="$AWS_REGION" AWS_PAGER='' AWS_CLI_AUTO_PROMPT=off
AWS=(aws --region "$AWS_REGION")
[[ -z "${AWS_PROFILE:-}" ]] || AWS+=(--profile "$AWS_PROFILE")
ACCOUNT="$("${AWS[@]}" sts get-caller-identity --query Account --output text)" || fail 'Credenciales AWS ausentes o expiradas.'
[[ "$ACCOUNT" == 573505212581 ]] || fail 'Se requieren credenciales de la cuenta 573505212581, propietaria de LabRole.'
OUT="$ROOT_DIR/.deploy-all-output"
mkdir -p -- "$OUT"
"${AWS[@]}" iam get-role --role-name LabRole --query 'Role.AssumeRolePolicyDocument' --output json > "$OUT/role-trust.json"
"$PYTHON_BIN" - "$OUT/role-trust.json" <<'PY'
import json,sys
policy=json.load(open(sys.argv[1]))
services=[]
for statement in policy.get('Statement',[]):
    if statement.get('Effect')!='Allow': continue
    value=statement.get('Principal',{}).get('Service',[])
    services.extend(value if isinstance(value,list) else [value])
missing={'lambda.amazonaws.com','states.amazonaws.com'}-set(services)
if missing: raise SystemExit('LabRole debe confiar en estos servicios: '+', '.join(sorted(missing))+'. Un administrador debe configurarlo.')
PY
printf 'Cuenta %s | región %s | stage %s | acción %s\n' "$ACCOUNT" "$AWS_REGION" "$STAGE" "$ACTION"
printf 'LabRole es existente: este script no modifica sus permisos.\n'

# Conserva la propiedad del bus de Telemetry igual que el script de 03/04/09.
ERROR_FILE="$OUT/aws-error.txt"
# Detectar una tabla previa incompatible antes de crear el primer stack.
if "${AWS[@]}" dynamodb describe-table --table-name redtelecorp-antennas --query 'Table.KeySchema' --output json > "$OUT/antenna-table.json" 2>"$ERROR_FILE"; then
  "$PYTHON_BIN" - "$OUT/antenna-table.json" <<'PY'
import json,sys
schema=json.load(open(sys.argv[1]))
if schema != [{'AttributeName':'antenna_id','KeyType':'HASH'}]:
    raise SystemExit('redtelecorp-antennas tiene una clave incompatible. Planificar migración; no se borrará la tabla.')
PY
  TABLE_OWNER="$("${AWS[@]}" cloudformation describe-stack-resource --stack-name "redtelecorp-network-infrastructure-$STAGE" --logical-resource-id AntennasTable --query StackResourceDetail.PhysicalResourceId --output text 2>"$ERROR_FILE")" || fail 'La tabla existe, pero no se pudo comprobar su propiedad en el stack 02. Revisar importación/reutilización; no se borrará.'
  [[ "$TABLE_OWNER" == redtelecorp-antennas ]] || fail 'El stack 02 no administra redtelecorp-antennas; revisar importación antes de desplegar.'
elif [[ "$(<"$ERROR_FILE")" != *ResourceNotFoundException* ]]; then
  cat "$ERROR_FILE" >&2
  fail 'No fue posible comprobar la tabla de antenas; un AccessDenied no significa que no exista.'
fi
OWNER=false
if OWNER="$("${AWS[@]}" cloudformation describe-stacks --stack-name "rtc-network-telemetry-$STAGE" --query 'Stacks[0].Parameters[?ParameterKey==`CreateEventBus`].ParameterValue | [0]' --output text 2>"$ERROR_FILE")"; then
  [[ "$OWNER" == true ]] || OWNER=false
elif [[ "$(<"$ERROR_FILE")" == *'does not exist'* ]]; then OWNER=false
else cat "$ERROR_FILE" >&2; fail 'No fue posible consultar el stack de Telemetry.'; fi
BUS_EXISTS=false
if "${AWS[@]}" events describe-event-bus --name "$EVENT_BUS_NAME" >/dev/null 2>"$ERROR_FILE"; then BUS_EXISTS=true
elif [[ "$(<"$ERROR_FILE")" != *ResourceNotFoundException* ]]; then cat "$ERROR_FILE" >&2; fail 'No fue posible consultar el bus.'; fi
case "$CREATE_EVENT_BUS" in
  auto) if [[ "$OWNER" == true || "$BUS_EXISTS" == false ]]; then CREATE_EVENT_BUS=true; else CREATE_EVENT_BUS=false; fi ;;
  true) [[ "$BUS_EXISTS" == false || "$OWNER" == true ]] || fail 'El bus existe fuera de este stack; no se puede reclamar automáticamente.' ;;
  false) [[ "$OWNER" == false ]] || fail 'No se puede retirar el bus propietario del stack.' ;;
  *) fail 'CREATE_EVENT_BUS debe ser auto, true o false.' ;;
esac

# La comprobación previa usa identificadores de validación, nunca se despliega con ellos.
POOL_ID='us-east-1_LocalValidation'
CLIENT_ID='localvalidationclient'
POOL_ARN="arn:aws:cognito-idp:$AWS_REGION:$ACCOUNT:userpool/$POOL_ID"
IDENTITY_URL=''
INFRASTRUCTURE_URL=''
TELEMETRY_URL=''
COVERAGE_URL=''
REPORTS_URL=''

endpoint() {
  local stack="$1" file="$OUT/$1.json"
  "${AWS[@]}" cloudformation describe-stacks --stack-name "$stack" --query 'Stacks[0].Outputs' --output json > "$file"
  "$PYTHON_BIN" - "$file" <<'PY'
import json,sys
outputs={x['OutputKey']:x['OutputValue'] for x in json.load(open(sys.argv[1]))}
value=outputs.get('ServiceEndpoint') or outputs.get('HttpApiUrl')
if not value: raise SystemExit('El stack no devolvió ServiceEndpoint/HttpApiUrl')
print(value.rstrip('/'))
PY
}

run_service() (
  local service="$1" action="$2" directory stack
  local args=(--stage "$STAGE" --region "$AWS_REGION" "--param=frontendOrigin=$FRONTEND_ORIGIN" "--param=eventBusName=$EVENT_BUS_NAME")
  if [[ "$service" != identity ]]; then
    args+=("--param=cognitoUserPoolArn=$POOL_ARN" "--param=cognitoUserPoolId=$POOL_ID" "--param=cognitoClientId=$CLIENT_ID")
  fi
  case "$service" in
    identity) directory="$ROOT_DIR/identity-access"; args+=("--param=adminEmails=$ADMIN_EMAILS") ;;
    infrastructure) directory="$ROOT_DIR/../MICROSERVICIO 2" ;;
    telemetry) directory="$ROOT_DIR/network-telemetry"; args+=("--param=createEventBus=$CREATE_EVENT_BUS"); [[ -z "$INFRASTRUCTURE_URL" ]] || args+=("--param=infrastructureApiUrl=$INFRASTRUCTURE_URL") ;;
    coverage) directory="$ROOT_DIR/coverage-geospatial"; args+=("--param=oracleEnabled=$ORACLE_ENABLED"); [[ -z "$INFRASTRUCTURE_URL" ]] || args+=("--param=infrastructureApiUrl=$INFRASTRUCTURE_URL"); [[ -z "${ORACLE_SECRET_ARN:-}" ]] || args+=("--param=oracleSecretArn=$ORACLE_SECRET_ARN") ;;
    reports) directory="$ROOT_DIR/../MICROSERVICIO 7" ;;
    field) directory="$ROOT_DIR/../MICROSERVICIO 8" ;;
    notifications) directory="$ROOT_DIR/notifications"; args+=("--param=deliveryEnabled=$DELIVERY_ENABLED" "--param=sesIdentity=${SES_IDENTITY:-unconfigured.invalid}"); [[ -z "$IDENTITY_URL" ]] || args+=("--param=identityApiUrl=$IDENTITY_URL"); [[ -z "$REPORTS_URL" ]] || args+=("--param=reportsApiUrl=$REPORTS_URL"); [[ -z "${SES_FROM_ADDRESS:-}" ]] || args+=("--param=sesFromAddress=$SES_FROM_ADDRESS") ;;
    incidents) directory="$ROOT_DIR/incidents-report"; args+=("--param=aiEnabled=$AI_ENABLED" "--param=collectiveReportThreshold=${COLLECTIVE_REPORT_THRESHOLD:-10}" "--param=requiredFieldSkill=${REQUIRED_FIELD_SKILL:-NETWORK}");
      [[ -z "$COVERAGE_URL" ]] || args+=("--param=coverageApiUrl=$COVERAGE_URL"); [[ -z "$TELEMETRY_URL" ]] || args+=("--param=telemetryApiUrl=$TELEMETRY_URL"); [[ -z "$REPORTS_URL" ]] || args+=("--param=reportsApiUrl=$REPORTS_URL");
      [[ -z "${GCP_AI_BASE_URL:-}" ]] || args+=("--param=gcpAiBaseUrl=$GCP_AI_BASE_URL"); [[ -z "${GCP_AI_SECRET_ARN:-}" ]] || args+=("--param=gcpAiSecretArn=$GCP_AI_SECRET_ARN") ;;
    *) fail 'Servicio desconocido.' ;;
  esac
  cd -- "$directory"
  printf '\n%s: %s\n' "$action" "$service"
  if [[ "$service" == incidents && ! -d node_modules/serverless-step-functions ]]; then
    [[ "$action" != print ]] || fail 'Instala las dependencias de 05: cd incidents-report && npm ci --ignore-scripts'
    npm ci --ignore-scripts --no-audit --no-fund
  fi
  if [[ "$service" == coverage && "$action" != print ]]; then "$PYTHON_BIN" handlers/post_impact_analysis.py --build-package; fi
  "$SERVERLESS_BIN" "$action" "${args[@]}"
)

if [[ ! -d "$ROOT_DIR/incidents-report/node_modules/serverless-step-functions" ]]; then
  (cd -- "$ROOT_DIR/incidents-report" && npm ci --ignore-scripts --no-audit --no-fund)
elif [[ "$ACTION" != check ]]; then
  (cd -- "$ROOT_DIR/incidents-report" && npm ci --ignore-scripts --no-audit --no-fund)
else
  (cd -- "$ROOT_DIR/incidents-report" && npm ls --depth=0 > "$OUT/incidents-dependencies.txt")
fi
for selection in identity telemetry infrastructure reports field coverage notifications incidents; do
  run_service "$selection" print > "$OUT/$selection-validation.txt"
done
if [[ "$ACTION" == check ]]; then printf '\nOcho configuraciones resueltas. No se crearon stacks. IAM de ejecución y proveedores externos requieren comprobación real.\n'; exit 0; fi
if [[ "$ACTION" == package ]]; then
  for selection in identity telemetry infrastructure reports field coverage notifications incidents; do run_service "$selection" package; done
  printf '\nPaquetes de comprobación creados con IDs locales; deploy vuelve a construir con los IDs reales.\n'; exit 0
fi

run_service identity deploy
POOL_ID="$("${AWS[@]}" ssm get-parameter --name "/telecorp/$STAGE/cognito/user-pool-id" --query Parameter.Value --output text)"
CLIENT_ID="$("${AWS[@]}" ssm get-parameter --name "/telecorp/$STAGE/cognito/client-id" --query Parameter.Value --output text)"
POOL_ARN="$("${AWS[@]}" ssm get-parameter --name "/telecorp/$STAGE/cognito/user-pool-arn" --query Parameter.Value --output text)"
[[ "$POOL_ID" != *LocalValidation* && "$CLIENT_ID" != localvalidationclient ]] || fail 'Identity no devolvió IDs reales.'
IDENTITY_URL="$(endpoint "telecorp-identity-service-$STAGE")"
run_service telemetry deploy
TELEMETRY_URL="$(endpoint "rtc-network-telemetry-$STAGE")"
run_service infrastructure deploy
INFRASTRUCTURE_URL="$(endpoint "redtelecorp-network-infrastructure-$STAGE")"
run_service telemetry deploy
run_service reports deploy
REPORTS_URL="$(endpoint "redtelecorp-customer-reports-$STAGE")"
run_service field deploy
FIELD_URL="$(endpoint "redtelecorp-field-operations-$STAGE")"
run_service coverage deploy
COVERAGE_URL="$(endpoint "rtc-coverage-geospatial-$STAGE")"
run_service notifications deploy
NOTIFICATIONS_URL="$(endpoint "rtc-notifications-$STAGE")"
run_service incidents deploy
INCIDENTS_URL="$(endpoint "telecorp-incidents-service-$STAGE")"
"$PYTHON_BIN" - "$ROOT_DIR/postman/RedTeleCorp.full-flow.environment.json" "$OUT/postman.environment.json" "$IDENTITY_URL" "$INFRASTRUCTURE_URL" "$TELEMETRY_URL" "$COVERAGE_URL" "$REPORTS_URL" "$FIELD_URL" "$INCIDENTS_URL" "$NOTIFICATIONS_URL" "$POOL_ID" "$CLIENT_ID" <<'PY'
import json,sys
source,target,*values=sys.argv[1:]
environment=json.load(open(source,encoding='utf-8'))
keys=['identity_base_url','infrastructure_base_url','telemetry_base_url','coverage_base_url','reports_base_url','field_base_url','incidents_base_url','notifications_base_url','cognito_user_pool_id','cognito_client_id']
settings=dict(zip(keys,values))
for item in environment['values']:
    if item['key'] in settings: item['value']=settings[item['key']]
json.dump(environment,open(target,'w',encoding='utf-8'),indent=2)
PY
printf '\nDespliegue AWS finalizado. Salidas reales en %s. Ejecuta la prueba de PRIMER_FLUJO_INTEGRADO.md antes de dar el flujo por validado.\n' "$OUT"
