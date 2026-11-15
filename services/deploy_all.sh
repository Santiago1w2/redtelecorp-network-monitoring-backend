#!/usr/bin/env bash
# Despliega el flujo canónico AWS desde EC2; 07 es legado opcional.
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
DEPLOY_LEGACY_REPORTS="${DEPLOY_LEGACY_REPORTS:-false}"
NETWORK_MODE="${NETWORK_MODE:-existing}"
ORACLE_TLS_PORT="${ORACLE_TLS_PORT:-1521}"
GCP_AUTH_MODE="${GCP_AUTH_MODE:-WIF}"
COLLECTIVE_REPORT_THRESHOLD="${COLLECTIVE_REPORT_THRESHOLD:-10}"
RECURRENT_ANTENNA_MIN_EVENTS="${RECURRENT_ANTENNA_MIN_EVENTS:-3}"
RECURRENT_ANTENNA_MIN_DAYS="${RECURRENT_ANTENNA_MIN_DAYS:-2}"
RECURRENT_ANTENNA_WINDOW_DAYS="${RECURRENT_ANTENNA_WINDOW_DAYS:-7}"
GCP_AI_API_KEY_PARAM="${GCP_AI_API_KEY_PARAM:-/telecorp/$STAGE/gcp/ai-api-key}"
EVENT_BUS_NAME="${EVENT_BUS_NAME:-rtc-events-$STAGE}"
CREATE_EVENT_BUS="${CREATE_EVENT_BUS:-auto}"
[[ "$STAGE" =~ ^[a-zA-Z0-9-]+$ ]] || fail 'STAGE invalido.'
[[ "$AWS_REGION" == us-east-1 ]] || fail 'La configuración inicial está preparada para us-east-1; usa esa región en todos los servicios.'
[[ "${FRONTEND_ORIGIN:-}" =~ ^https://[^/?\#[:space:]]+$ && "$FRONTEND_ORIGIN" != *REEMPLAZAR* ]] || fail 'Configura FRONTEND_ORIGIN con el origen HTTPS real.'
[[ -n "${ADMIN_EMAILS:-}" && "$ADMIN_EMAILS" == *@* && "$ADMIN_EMAILS" != *REEMPLAZAR* ]] || fail 'Configura ADMIN_EMAILS con los correos administradores separados por coma.'
for flag in "$AI_ENABLED" "$ORACLE_ENABLED" "$DELIVERY_ENABLED" "$DEPLOY_LEGACY_REPORTS"; do
  case "$flag" in true|false) ;; *) fail 'Los indicadores de integración admiten true/false.' ;; esac
done
[[ "$AI_ENABLED" != true || -n "${GCP_AI_BASE_URL:-}" ]] || fail 'IA requiere GCP_AI_BASE_URL y clave en SSM SecureString o Secrets Manager.'
[[ "$GCP_AUTH_MODE" == WIF || "$GCP_AUTH_MODE" == API_KEY ]] || fail 'GCP_AUTH_MODE debe ser WIF o API_KEY.'
[[ "$COLLECTIVE_REPORT_THRESHOLD" =~ ^[1-9][0-9]{0,3}$ ]] || fail 'COLLECTIVE_REPORT_THRESHOLD debe ser un entero entre 1 y 9999.'
[[ "$RECURRENT_ANTENNA_MIN_EVENTS" =~ ^([2-9]|[1-9][0-9]{1,2})$ ]] || fail 'RECURRENT_ANTENNA_MIN_EVENTS debe ser un entero entre 2 y 999.'
[[ "$RECURRENT_ANTENNA_MIN_DAYS" =~ ^([2-9]|[1-9][0-9]{1,2})$ ]] || fail 'RECURRENT_ANTENNA_MIN_DAYS debe ser un entero entre 2 y 999.'
[[ "$RECURRENT_ANTENNA_WINDOW_DAYS" =~ ^([2-9]|[1-9][0-9]{1,2})$ ]] || fail 'RECURRENT_ANTENNA_WINDOW_DAYS debe ser un entero entre 2 y 31.'
(( RECURRENT_ANTENNA_WINDOW_DAYS <= 31 )) || fail 'RECURRENT_ANTENNA_WINDOW_DAYS no puede superar 31 dias.'
(( RECURRENT_ANTENNA_MIN_DAYS <= RECURRENT_ANTENNA_WINDOW_DAYS )) || fail 'RECURRENT_ANTENNA_MIN_DAYS no puede superar la ventana de recurrencia.'
[[ "$AI_ENABLED" != true || "$GCP_AUTH_MODE" != WIF || ( -n "${GCP_WIF_AUDIENCE:-}" && -n "${GCP_INVOKER_SERVICE_ACCOUNT:-}" ) ]] || fail 'Cloud Run privado requiere GCP_WIF_AUDIENCE y GCP_INVOKER_SERVICE_ACCOUNT.'
[[ "$ORACLE_ENABLED" != true || -n "${ORACLE_SECRET_ARN:-}" ]] || fail 'Oracle requiere ORACLE_SECRET_ARN y red configurada.'
case "$NETWORK_MODE" in
  existing)
    [[ "${NETWORK_VPC_ID:-}" =~ ^vpc-[a-f0-9]+$ && "${ORACLE_SECURITY_GROUP_IDS:-}" =~ ^sg-[a-f0-9]+(,sg-[a-f0-9]+)*$ && "${ORACLE_SUBNET_IDS:-}" =~ ^subnet-[a-f0-9]+(,subnet-[a-f0-9]+)*$ && -n "${NETWORK_EGRESS_IP:-}" ]] || fail 'NETWORK_MODE=existing requiere NETWORK_VPC_ID, ORACLE_SECURITY_GROUP_IDS, ORACLE_SUBNET_IDS y NETWORK_EGRESS_IP. Copia el bloque de red del ejemplo.' ;;
  create) ;;
  *) fail 'NETWORK_MODE debe ser existing o create.' ;;
esac
[[ "$ORACLE_TLS_PORT" =~ ^[0-9]{1,5}$ ]] && (( 10#$ORACLE_TLS_PORT >= 1 && 10#$ORACLE_TLS_PORT <= 65535 )) || fail 'ORACLE_TLS_PORT debe estar entre 1 y 65535.'
# Los IDs de comprobacion nunca se utilizan para desplegar Coverage.
if [[ "$NETWORK_MODE" == create ]]; then
  ORACLE_SECURITY_GROUP_IDS='sg-00000000000000000'
  ORACLE_SUBNET_IDS='subnet-00000000000000000,subnet-00000000000000001'
fi
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
LEGACY_REPORTS_URL=''

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
  if [[ "$service" != identity && "$service" != network ]]; then
    args+=("--param=cognitoUserPoolArn=$POOL_ARN" "--param=cognitoUserPoolId=$POOL_ID" "--param=cognitoClientId=$CLIENT_ID")
  fi
  case "$service" in
    network)
      directory="$ROOT_DIR/network"
      args+=("--param=networkMode=$NETWORK_MODE" "--param=oracleTlsPort=$ORACLE_TLS_PORT")
      if [[ "$NETWORK_MODE" == existing ]]; then
        args+=("--param=existingVpcId=$NETWORK_VPC_ID" "--param=existingSecurityGroupIds=$ORACLE_SECURITY_GROUP_IDS" "--param=existingSubnetIds=$ORACLE_SUBNET_IDS" "--param=existingEgressIp=$NETWORK_EGRESS_IP")
      fi ;;
    identity) directory="$ROOT_DIR/identity-access"; args+=("--param=adminEmails=$ADMIN_EMAILS") ;;
    infrastructure) directory="$ROOT_DIR/../MICROSERVICIO 2" ;;
    telemetry) directory="$ROOT_DIR/network-telemetry"; args+=("--param=createEventBus=$CREATE_EVENT_BUS"); [[ -z "$INFRASTRUCTURE_URL" ]] || args+=("--param=infrastructureApiUrl=$INFRASTRUCTURE_URL") ;;
    coverage) directory="$ROOT_DIR/coverage-geospatial"; args+=("--param=oracleEnabled=$ORACLE_ENABLED");
      [[ -z "${ORACLE_SECURITY_GROUP_IDS:-}" ]] || args+=("--param=oracleSecurityGroupIds=$ORACLE_SECURITY_GROUP_IDS");
      [[ -z "${ORACLE_SUBNET_IDS:-}" ]] || args+=("--param=oracleSubnetIds=$ORACLE_SUBNET_IDS");
      [[ -z "$INFRASTRUCTURE_URL" ]] || args+=("--param=infrastructureApiUrl=$INFRASTRUCTURE_URL"); [[ -z "${ORACLE_SECRET_ARN:-}" ]] || args+=("--param=oracleSecretArn=$ORACLE_SECRET_ARN") ;;
    reports) directory="$ROOT_DIR/../MICROSERVICIO 7" ;;
    field) directory="$ROOT_DIR/../MICROSERVICIO 8" ;;
    notifications) directory="$ROOT_DIR/notifications"; args+=("--param=deliveryEnabled=$DELIVERY_ENABLED"); [[ -z "$IDENTITY_URL" ]] || args+=("--param=identityApiUrl=$IDENTITY_URL"); [[ -z "$REPORTS_URL" ]] || args+=("--param=reportsApiUrl=$REPORTS_URL") ;;
    incidents) directory="$ROOT_DIR/incidents-report"; args+=("--param=aiEnabled=$AI_ENABLED" "--param=collectiveReportThreshold=$COLLECTIVE_REPORT_THRESHOLD" "--param=recurrentAntennaMinEvents=$RECURRENT_ANTENNA_MIN_EVENTS" "--param=recurrentAntennaMinDays=$RECURRENT_ANTENNA_MIN_DAYS" "--param=recurrentAntennaWindowDays=$RECURRENT_ANTENNA_WINDOW_DAYS" "--param=requiredFieldSkill=${REQUIRED_FIELD_SKILL:-NETWORK}" "--param=gcpAiApiKeyParam=$GCP_AI_API_KEY_PARAM" "--param=gcpAuthMode=$GCP_AUTH_MODE");
      [[ -z "${GCP_WIF_AUDIENCE:-}" ]] || args+=("--param=gcpWifAudience=$GCP_WIF_AUDIENCE");
      [[ -z "${GCP_INVOKER_SERVICE_ACCOUNT:-}" ]] || args+=("--param=gcpInvokerServiceAccount=$GCP_INVOKER_SERVICE_ACCOUNT");
      [[ -z "$COVERAGE_URL" ]] || args+=("--param=coverageApiUrl=$COVERAGE_URL"); [[ -z "$TELEMETRY_URL" ]] || args+=("--param=telemetryApiUrl=$TELEMETRY_URL"); [[ -z "$LEGACY_REPORTS_URL" ]] || args+=("--param=legacyReportsApiUrl=$LEGACY_REPORTS_URL");
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
SELECTIONS=(network identity telemetry infrastructure field coverage incidents notifications)
[[ "$DEPLOY_LEGACY_REPORTS" != true ]] || SELECTIONS+=(reports)
for selection in "${SELECTIONS[@]}"; do
  run_service "$selection" print > "$OUT/$selection-validation.txt"
done
if [[ "$ACTION" == check ]]; then printf '\nConfiguraciones resueltas; 05 es canónico y 07 es legado opcional. No se crearon stacks. Verificar permisos y proveedores reales.\n'; exit 0; fi
if [[ "$ACTION" == package ]]; then
  for selection in "${SELECTIONS[@]}"; do run_service "$selection" package; done
  printf '\nPaquetes de comprobación creados con IDs locales; deploy vuelve a construir con los IDs reales.\n'; exit 0
fi

# No cambiar el modo de un stack existente: quitar condiciones podria borrar su NAT/VPC.
if "${AWS[@]}" cloudformation describe-stacks --stack-name "rtc-network-$STAGE" --query 'Stacks[0].Outputs' --output json > "$OUT/network-previous.json" 2>"$ERROR_FILE"; then
  PREVIOUS_MODE="$("$PYTHON_BIN" - "$OUT/network-previous.json" <<'PY'
import json,sys
print(next((x['OutputValue'] for x in json.load(open(sys.argv[1])) if x['OutputKey']=='NetworkMode'), 'unknown'))
PY
)"
  [[ "$PREVIOUS_MODE" == "$NETWORK_MODE" ]] || fail 'El stack de red tiene otro modo. Planifica la migracion; no se sustituira ni eliminara automaticamente.'
elif [[ "$(<"$ERROR_FILE")" != *'does not exist'* ]]; then
  cat "$ERROR_FILE" >&2
  fail 'No fue posible comprobar el stack de red; no se creara otra red.'
fi
run_service network deploy
"${AWS[@]}" cloudformation describe-stacks --stack-name "rtc-network-$STAGE" --query 'Stacks[0].Outputs' --output json > "$OUT/network.json"
"$PYTHON_BIN" - "$OUT/network.json" "$OUT/network.env" <<'PY'
import ipaddress,json,re,sys
outputs={x['OutputKey']:x['OutputValue'] for x in json.load(open(sys.argv[1]))}
patterns={'VpcId':r'vpc-[a-f0-9]+','SecurityGroupIds':r'sg-[a-f0-9]+(?:,sg-[a-f0-9]+)*','SubnetIds':r'subnet-[a-f0-9]+(?:,subnet-[a-f0-9]+)*'}
for key,pattern in patterns.items():
    if not re.fullmatch(pattern,outputs.get(key,'')): raise SystemExit('Salida de red invalida: '+key)
    if '0000000000000000' in outputs[key]: raise SystemExit('La red devolvio IDs de validacion')
ipaddress.IPv4Address(outputs['OracleEgressIp'])
names={'NETWORK_VPC_ID':'VpcId','ORACLE_SECURITY_GROUP_IDS':'SecurityGroupIds','ORACLE_SUBNET_IDS':'SubnetIds','NETWORK_EGRESS_IP':'OracleEgressIp'}
with open(sys.argv[2],'w') as result:
    for name,key in names.items(): result.write(name+'='+outputs[key]+'\n')
PY
source "$OUT/network.env"
printf 'Red lista | VPC %s | IP de salida para autorizar en Oracle: %s\n' "$NETWORK_VPC_ID" "$NETWORK_EGRESS_IP"
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
if [[ "$DEPLOY_LEGACY_REPORTS" == true ]]; then
  run_service reports deploy
  LEGACY_REPORTS_URL="$(endpoint "redtelecorp-customer-reports-$STAGE")"
fi
run_service field deploy
FIELD_URL="$(endpoint "redtelecorp-field-operations-$STAGE")"
run_service coverage deploy
COVERAGE_URL="$(endpoint "rtc-coverage-geospatial-$STAGE")"
run_service incidents deploy
INCIDENTS_URL="$(endpoint "telecorp-incidents-service-$STAGE")"
REPORTS_URL="$INCIDENTS_URL"
run_service notifications deploy
NOTIFICATIONS_URL="$(endpoint "rtc-notifications-$STAGE")"
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
