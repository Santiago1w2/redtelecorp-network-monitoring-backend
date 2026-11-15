"""Execute the deployment orchestrator against CLI doubles; never call AWS."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

SERVICES = Path(__file__).resolve().parents[1]
BASH = ('C:/Program Files/Git/bin/bash.exe' if os.name == 'nt' and Path('C:/Program Files/Git/bin/bash.exe').exists() else shutil.which('bash'))
pytestmark = pytest.mark.skipif(not BASH, reason='Bash required')


@pytest.fixture
def deployment(tmp_path):
    root = tmp_path / 'services'
    root.mkdir()
    (root / 'deploy_all.sh').write_text((SERVICES / 'deploy_all.sh').read_text(encoding='utf-8'), encoding='utf-8', newline='\n')
    for folder in ['network', 'identity-access', 'network-telemetry', 'coverage-geospatial', 'incidents-report', 'notifications']:
        (root / folder).mkdir()
    for folder in ['MICROSERVICIO 2', 'MICROSERVICIO 8']:
        (tmp_path / folder).mkdir()
    (root / 'coverage-geospatial/handlers').mkdir()
    (root / 'coverage-geospatial/handlers/post_impact_analysis.py').write_text('pass\n')
    (root / 'incidents-report/node_modules/serverless-step-functions').mkdir(parents=True)
    (root / 'postman').mkdir()
    (root / 'postman/RedTeleCorp.full-flow.environment.json').write_text('{"values":[]}')
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    scripts = {
        'aws': r'''#!/usr/bin/env bash
set -eu
case "$*" in
 *'sts get-caller-identity'*) echo 573505212581 ;;
 *'iam get-role'*) echo '{"Statement":[{"Effect":"Allow","Principal":{"Service":["lambda.amazonaws.com","states.amazonaws.com"]}}]}' ;;
 *'dynamodb describe-table'*) echo ResourceNotFoundException >&2; exit 254 ;;
 *'events describe-event-bus'*) echo '{}' ;;
 *'ssm get-parameter'*)
   case "$*" in
    *cognito/user-pool-id*) echo us-east-1_TestPool ;;
    *cognito/client-id*) echo actualclient ;;
    *) echo arn:aws:cognito-idp:us-east-1:573505212581:userpool/us-east-1_TestPool ;;
   esac ;;
 *'cloudformation describe-stacks'*)
   case "$*" in
    *rtc-network-dev*)
      if [[ ! -f "$TEST_STATE" && -z "${TEST_PREVIOUS_MODE:-}" ]]; then echo 'Stack does not exist' >&2; exit 254; fi
      mode="${TEST_PREVIOUS_MODE:-$TEST_MODE}"
      printf '[{"OutputKey":"NetworkMode","OutputValue":"%s"},{"OutputKey":"VpcId","OutputValue":"vpc-1234567890abcdef0"},{"OutputKey":"SecurityGroupIds","OutputValue":"sg-1234567890abcdef0"},{"OutputKey":"SubnetIds","OutputValue":"subnet-11111111111111111,subnet-22222222222222222"},{"OutputKey":"OracleEgressIp","OutputValue":"198.51.100.12"}]\n' "$mode" ;;
    *CreateEventBus*) echo false ;;
    *) echo '[{"OutputKey":"ServiceEndpoint","OutputValue":"https://api.example.test/dev"}]' ;;
   esac ;;
 *) echo "Unexpected fake AWS request" >&2; exit 99 ;;
esac
''',
        'serverless': r'''#!/usr/bin/env bash
set -eu
printf '%s|%s\n' "${PWD##*/}" "$*" >> "$TEST_LOG"
if [[ "${PWD##*/}" == network && "$1" == deploy ]]; then
 [[ "${TEST_FAIL_NETWORK:-false}" != true ]] || exit 42
 touch "$TEST_STATE"
fi
''',
    }
    for name in ['node', 'npm', 'uv']:
        scripts[name] = '#!/usr/bin/env bash\nexit 0\n'
    for name, body in scripts.items():
        file = bin_dir / name
        file.write_text(body, encoding='utf-8', newline='\n')
        file.chmod(0o755)
    env_file = root / 'deploy_all.env'
    env_file.write_text("\n".join([
        'STAGE=dev', 'AWS_REGION=us-east-1', 'FRONTEND_ORIGIN=https://example.test',
        'ADMIN_EMAILS=admin@example.test', 'ORACLE_ENABLED=true', 'ORACLE_SECRET_ARN=arn:aws:secretsmanager:us-east-1:573505212581:secret:test',
        'NETWORK_MODE=${TEST_MODE}', 'NETWORK_VPC_ID=vpc-abcdef12345678900', 'NETWORK_EGRESS_IP=198.51.100.10',
        'ORACLE_SECURITY_GROUP_IDS=sg-abcdef12345678900', 'ORACLE_SUBNET_IDS=subnet-abcdef12345678900',
        "PYTHON_BIN='" + sys.executable.replace('\\', '/') + "'",
        "SERVERLESS_BIN='" + str(bin_dir / 'serverless').replace('\\', '/') + "'",
    ]) + '\n', encoding='utf-8', newline='\n')
    log = tmp_path / 'calls.log'
    env = dict(os.environ, TEST_LOG=log.as_posix(), TEST_STATE=(tmp_path / 'network-created').as_posix())
    # Let Bash prepend its own platform-native PATH to avoid Windows PATH separators.
    def execute(mode='existing', action='deploy', **flags):
        env.update(TEST_MODE=mode, **flags)
        bin_path = bin_dir.as_posix()
        if os.name == 'nt':
            bin_path = '/' + bin_path[0].lower() + bin_path[2:]
        command = 'export PATH="' + bin_path + ':$PATH"\nexec bash deploy_all.sh "' + action + '"'
        result = subprocess.run([BASH, '-c', command], cwd=root, env=env, text=True, capture_output=True, timeout=40)
        return result, log.read_text().splitlines() if log.exists() else []
    return execute


@pytest.mark.parametrize('mode', ['existing', 'create'])
def test_network_deploys_before_apis_and_coverage_uses_real_outputs(deployment, mode):
    result, calls = deployment(mode)
    assert result.returncode == 0, result.stdout + result.stderr
    deploys = [line for line in calls if '|deploy ' in line]
    assert deploys[0].startswith('network|deploy ')
    coverage = next(line for line in deploys if line.startswith('coverage-geospatial|'))
    assert '--param=oracleSecurityGroupIds=sg-1234567890abcdef0' in coverage
    assert '--param=oracleSubnetIds=subnet-11111111111111111,subnet-22222222222222222' in coverage
    assert '0000000000000000' not in coverage
    if mode == 'create':
        assert 'existingSecurityGroupIds' not in deploys[0]


def test_failed_network_stops_all_api_deployments(deployment):
    result, calls = deployment(TEST_FAIL_NETWORK='true')
    assert result.returncode != 0
    assert [line.split('|')[0] for line in calls if '|deploy ' in line] == ['network']


def test_cannot_switch_existing_stack_to_another_mode(deployment):
    result, calls = deployment('create', TEST_PREVIOUS_MODE='existing')
    assert result.returncode != 0
    assert 'otro modo' in result.stderr
    assert not any('|deploy ' in line for line in calls)


def test_check_never_deploys_resources(deployment):
    result, calls = deployment('create', action='check')
    assert result.returncode == 0, result.stdout + result.stderr
    assert calls[0].startswith('network|print ')
    assert not any('|deploy ' in line for line in calls)
