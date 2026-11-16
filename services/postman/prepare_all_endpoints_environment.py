"""Carga solo URLs e IDs publicos desde la salida del despliegue; nunca copia secretos."""
import argparse
import json
from pathlib import Path

base = Path(__file__).resolve().parent
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=base.parent / '.deploy-all-output/postman.environment.json')
parser.add_argument('--output', type=Path, default=base.parent / '.deploy-all-output/all-endpoints.environment.json')
args = parser.parse_args()
if not args.source.is_file():
    parser.error('No existe la salida de despliegue: ' + str(args.source))
template = json.loads((base / 'RedTeleCorp.all-endpoints.postman_environment.json').read_text(encoding='utf-8-sig'))
source = json.loads(args.source.read_text(encoding='utf-8-sig'))
allowed = {v['key'] for v in template['values'] if v['key'].endswith('_base_url') or v['key'] in ('cognito_user_pool_id', 'cognito_client_id')}
settings = {v['key']: v.get('value', '') for v in source.get('values', []) if v['key'] in allowed}
for item in template['values']:
    if item['key'] in settings:
        item['value'] = settings[item['key']]
template['name'] = 'Red TeleCorp - TODOS los endpoints (URLs del despliegue)'
args.output.parent.mkdir(parents=True, exist_ok=True)
args.output.write_text(json.dumps(template, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print('Entorno preparado:', args.output)
print('Completar IdToken/user_id localmente. Credenciales AWS y contrasenas permanecen vacias.')
