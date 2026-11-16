"""Seed a resumable, synthetic Lima evidence dataset through public dev APIs only.

No direct DynamoDB writes, AWS keys, real phone numbers or credentials in Git.
"""
import argparse
import base64
import csv
from datetime import datetime, timedelta, timezone
import getpass
import json
import os
from pathlib import Path
import random
import secrets
import time
from urllib import request, error, parse

BASES = {
    'identity': 'https://p9s8rma5hk.execute-api.us-east-1.amazonaws.com/dev',
    'infrastructure': 'https://vxj8wme5da.execute-api.us-east-1.amazonaws.com',
    'coverage': 'https://0hvgxo44w9.execute-api.us-east-1.amazonaws.com/dev',
    'telemetry': 'https://6s5vvqxpg2.execute-api.us-east-1.amazonaws.com/dev',
    'incidents': 'https://95jltzhn9d.execute-api.us-east-1.amazonaws.com/dev',
    'field': 'https://hrui93k9e4.execute-api.us-east-1.amazonaws.com',
    'notifications': 'https://7bd55gl5b6.execute-api.us-east-1.amazonaws.com/dev',
}
DISTRICTS = [
    ('Cercado de Lima', -12.0464, -77.0428), ('San Martin de Porres', -12.009, -77.086),
    ('Los Olivos', -11.991, -77.071), ('Comas', -11.947, -77.058), ('Carabayllo', -11.891, -77.029),
    ('San Juan de Lurigancho', -12.001, -76.996), ('El Agustino', -12.044, -76.997),
    ('Ate', -12.055, -76.947), ('Santa Anita', -12.043, -76.971), ('La Molina', -12.087, -76.940),
    ('San Borja', -12.098, -76.995), ('Santiago de Surco', -12.131, -76.985),
    ('Miraflores', -12.121, -77.030), ('Barranco', -12.149, -77.022), ('Chorrillos', -12.170, -77.013),
    ('Villa El Salvador', -12.217, -76.940), ('Villa Maria del Triunfo', -12.164, -76.947),
    ('San Juan de Miraflores', -12.163, -76.972), ('San Miguel', -12.077, -77.087),
    ('Callao', -12.052, -77.122),
]
ISO = lambda value: value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def http(url, method='GET', body=None, token=None, headers=None):
    req = request.Request(url, method=method, data=json.dumps(body).encode() if body is not None else None,
                          headers={'Content-Type': 'application/json', **({'Authorization': token} if token else {}), **(headers or {})})
    try:
        with request.urlopen(req, timeout=40) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except error.HTTPError as exc:
        raw = exc.read(200000).decode(errors='replace')
        try:
            value = json.loads(raw)
        except ValueError:
            value = {'message': raw[:300]}
        return exc.code, value


def cognito(action, payload):
    return http('https://cognito-idp.us-east-1.amazonaws.com/', 'POST', payload,
                headers={'Content-Type': 'application/x-amz-json-1.1',
                         'X-Amz-Target': 'AWSCognitoIdentityProviderService.' + action})


def login(client, email, password, final_password=None):
    code, auth = cognito('InitiateAuth', {'AuthFlow': 'USER_PASSWORD_AUTH', 'ClientId': client,
                                        'AuthParameters': {'USERNAME': email, 'PASSWORD': password}})
    if code == 200 and auth.get('ChallengeName') == 'NEW_PASSWORD_REQUIRED' and final_password:
        code, auth = cognito('RespondToAuthChallenge', {'ClientId': client, 'ChallengeName': 'NEW_PASSWORD_REQUIRED',
                            'Session': auth['Session'], 'ChallengeResponses': {
                                'USERNAME': auth.get('ChallengeParameters', {}).get('USER_ID_FOR_SRP', email),
                                'NEW_PASSWORD': final_password}})
    token = auth.get('AuthenticationResult', {}).get('IdToken')
    if code != 200 or not token:
        raise RuntimeError(f'Cognito {email}: HTTP {code}, {auth.get("__type", auth.get("ChallengeName", "sin token"))}')
    return token


def claims(token):
    raw = token.split('.')[1]
    return json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))


def make_plan(ids, now):
    if len(ids) != 20 or len(set(ids)) != 20:
        raise ValueError('Se requieren exactamente los 20 IDs distintos de antenas')
    roles = ['admin'] + ['supervisor_red'] * 4 + ['operador_soporte'] * 5 + ['tecnico_campo'] * 30 + ['clientes'] * 60
    users = [{'email': f'rtc.evidencia.lima.{i+1:03d}@example.invalid', 'role': role} for i, role in enumerate(roles)]
    zones, stations, antennas, measurements, reports = [], [], [], [], []
    types = [('HEALTHCARE', 'CRITICAL', 'Centro medico simulado'), ('EDUCATION', 'HIGH', 'Campus simulado'),
             ('RESIDENTIAL', 'MEDIUM', 'Sector residencial simulado'), ('INDUSTRIAL', 'HIGH', 'Sector industrial simulado'),
             ('COMMERCIAL', 'LOW', 'Sector comercial simulado')]
    offsets = [(0, 0), (.006, 0), (0, .006), (-.006, 0), (0, -.006)]
    for district_index, (district, lat, lon) in enumerate(DISTRICTS):
        for sector, (dlat, dlon) in enumerate(offsets):
            index = len(zones) + 1
            latitude, longitude = round(lat + dlat, 6), round(lon + dlon, 6)
            zone_id, station_id = f'ZONE-EVID-LIMA-{index:03d}', f'STA-EVID-LIMA-{index:03d}'
            kind, criticality, label = types[sector]
            h = .0035
            ring = [[longitude-h, latitude-h], [longitude+h, latitude-h], [longitude+h, latitude+h], [longitude-h, latitude+h], [longitude-h, latitude-h]]
            zones.append({'zone_id': zone_id, 'name': f'EVIDENCIA SIMULADA - {district} - {label}',
                          'zone_type': kind, 'criticality': criticality, 'geometry': {'type': 'Polygon', 'coordinates': [ring]}})
            stations.append({'stationId': station_id, 'name': f'EVIDENCIA - {district} {sector+1:02d}',
                             'zoneId': zone_id, 'latitude': latitude, 'longitude': longitude, 'status': 'ACTIVE'})
        antennas.append({'antenna_id': ids[district_index], 'stationId': stations[district_index*5]['stationId'],
                         'name': f'Antena SIM {district_index+1:02d} - {district}', 'technology': '4G' if district_index % 2 else '5G', 'status': 'ACTIVE'})
    rng = random.Random(20261115)
    for sample in range(5):
        for i, antenna in enumerate(antennas):
            if sample == 4:
                value = 0 if i in (3, 19) else rng.randint(15, 55) if i in (0, 1, 2) else rng.randint(135, 175) if i in (5, 7) else rng.randint(65, 125)
            else:
                value = rng.randint(15, 55) if i < 3 and sample % 2 == 0 else rng.randint(135, 175) if i == 5 and sample % 2 == 0 else rng.randint(65, 125)
            measurements.append({'antenna_id': antenna['antenna_id'], 'traffic_gb_min': value,
                                 'timestamp': ISO(now - timedelta(days=4-sample, seconds=i))})
    for i in range(100):
        # First three cohorts: 20 distinct customers each, same zone/type, <24h.
        if i < 60:
            zone_index, customer_index, problem = (i // 20) * 5, 40 + i, 'SIN_INTERNET'
            occurred = now - timedelta(hours=2, minutes=i % 20)
        else:
            zone_index, customer_index = i - 45, 40 + ((i - 60) % 60)
            problem = ['LENTITUD', 'LLAMADAS', 'MENSAJES', 'SIN_COBERTURA'][i % 4]
            occurred = now - timedelta(days=(i % 4), minutes=i)
        reports.append({'customer_index': customer_index, 'idempotency_key': f'rtc-evidencia-lima-report-{i+1:03d}', 'body': {
            'zona': zones[zone_index]['zone_id'], 'numeroServicio': f'SIM-SERV-{customer_index+1:03d}',
            'tipoProblema': problem, 'fechaHora': ISO(occurred),
            'descripcion': f'[EVIDENCIA FICTICIA {i+1:03d}] {problem}: interrupcion simulada de conectividad para la demostracion Red TeleCorp en {zones[zone_index]["name"]}. No corresponde a un cliente ni incidente real.'}})
    return {'dataset': 'RTC-EVIDENCIA-LIMA-20261115-v1', 'created_at': ISO(now), 'fictional': True,
            'users': users, 'zones': zones, 'stations': stations, 'antennas': antennas,
            'measurements': measurements, 'reports': reports}


class Seeder:
    def __init__(self, directory, plan, client, admin_token):
        self.directory, self.plan, self.client, self.admin = directory, plan, client, admin_token
        self.state_path = directory / 'progress.json'
        self.state = json.loads(self.state_path.read_text(encoding='utf-8')) if self.state_path.exists() else {'done': {}, 'responses': {}, 'errors': []}
        self.secret_path = directory / 'CREDENCIALES_PRIVADAS.json'
        self.credentials = json.loads(self.secret_path.read_text(encoding='utf-8')) if self.secret_path.exists() else {}
        self.tokens = {}

    def call(self, service, path, method='GET', body=None, token=None, headers=None, expect=(200,)):
        code, result = http(BASES[service] + path, method, body, token or self.admin, headers)
        if code not in expect:
            raise RuntimeError(f'{service} {method} {path}: HTTP {code}: {str(result)[:300]}')
        return code, result

    def record(self, key, result):
        self.state['done'][key] = ISO(datetime.now(timezone.utc))
        self.state['responses'][key] = result
        save(self.state_path, self.state)

    def create_resource(self, key, service, collection, body, identifier):
        if key in self.state['done']:
            return
        code, value = self.call(service, collection, 'POST', body, expect=(200, 201, 409))
        if code == 409:
            _, existing = self.call(service, collection + '/' + parse.quote(body[identifier]))
            # Existing objects may contain generated fields, but must match the planned data.
            expected = {k: v for k, v in body.items() if k != 'geometry'}
            actual = existing.get('properties', existing)
            if any(actual.get(k) != v for k, v in expected.items()):
                raise RuntimeError('Objeto existente distinto; no se sobrescribe: ' + key)
        self.record(key, value)

    def seed_users(self):
        for i, user in enumerate(self.plan['users']):
            email, role = user['email'], user['role']
            entry = self.credentials.get(email)
            if not entry:
                entry = {**user, 'temporary_password': 'Rtc!' + secrets.token_urlsafe(17) + '8aA', 'password': 'Rtc!' + secrets.token_urlsafe(18) + '9bB'}
                self.credentials[email] = entry
                save(self.secret_path, self.credentials)
            key = 'user:' + email
            if key not in self.state['done']:
                code, value = http(BASES['identity'] + '/auth/usuarios', 'POST',
                                   {**user, 'password': entry['temporary_password']}, self.admin)
                if code != 201:
                    # A previous interrupted request may have completed; verify by logging in.
                    try:
                        token = login(self.client, email, entry['password'])
                    except RuntimeError:
                        if code != 500:
                            raise RuntimeError(f'Crear usuario ficticio {email}: HTTP {code}')
                        token = login(self.client, email, entry['temporary_password'], entry['password'])
                else:
                    token = login(self.client, email, entry['temporary_password'], entry['password'])
                _, profile = self.call('identity', '/auth/perfil', token=token)
                if profile.get('user', {}).get('role') != role:
                    raise RuntimeError('Rol no confirmado: ' + email)
                entry['user_id'] = profile['user']['userId']
                save(self.secret_path, self.credentials)
                self.record(key, {'email': email, 'role': role, 'user_id': entry['user_id']})
                self.tokens[email] = token
            if (i + 1) % 10 == 0:
                print(f'Usuarios confirmados: {i+1}/100', flush=True)

    def user_token(self, index):
        email = self.plan['users'][index]['email']
        if email not in self.tokens:
            self.tokens[email] = login(self.client, email, self.credentials[email]['password'])
        return self.tokens[email]

    def seed_base(self):
        for kind, service, path, field in [('zones', 'coverage', '/v1/coverage/zones', 'zone_id'), ('stations', 'infrastructure', '/stations', 'stationId'), ('antennas', 'infrastructure', '/antennas', 'antenna_id')]:
            for i, item in enumerate(self.plan[kind]):
                self.create_resource(kind + ':' + item[field], service, path, item, field)
                if (i + 1) % 10 == 0:
                    print(f'{kind}: {i+1}/{len(self.plan[kind])}', flush=True)
        for i, antenna in enumerate(self.plan['antennas']):
            station = self.plan['stations'][i * 5]
            key = 'coverage:' + antenna['antenna_id']
            if key not in self.state['done']:
                data = {'latitude': station['latitude'], 'longitude': station['longitude'], 'zone_id': station['zoneId'],
                        'coverage_radius_m': 1400 + (i % 4) * 250, 'coverage_model': 'ESTIMATED_CIRCLE'}
                _, value = self.call('coverage', '/v1/coverage/antennas/' + antenna['antenna_id'], 'PUT', data)
                self.record(key, value)
        print('Coberturas: 20/20 en Lima y Callao', flush=True)
        for index, user in enumerate(self.plan['users']):
            if user['role'] == 'tecnico_campo':
                user_id = self.credentials[user['email']]['user_id']
                zone = self.plan['zones'][(index - 10) % 3 * 5]['zone_id']
                self.create_resource('technician:' + user_id, 'field', '/technicians', {
                    'technicianId': user_id, 'name': f'Tecnico ficticio Lima {index-9:02d}', 'zoneId': zone,
                    'skills': ['NETWORK', '4G', '5G'], 'status': 'AVAILABLE'}, 'technicianId')
        print('Perfiles de tecnicos: 30/30 vinculados a cuentas', flush=True)

    def seed_measurements(self):
        # Publish chronologically, one day per batch; IDs derived by backend deduplicate retries.
        for day in range(5):
            key = f'telemetry-batch:{day}'
            if key not in self.state['done']:
                data = self.plan['measurements'][day*20:(day+1)*20]
                _, value = self.call('telemetry', '/v1/telemetry/batch', 'POST', {'measurements': data}, expect=(202, 207))
                if value.get('failed') or len(value.get('accepted', [])) != 20:
                    raise RuntimeError('Ingesta parcial; no se marca completa: ' + str(value)[:300])
                self.record(key, value)
            print(f'Mediciones aceptadas: {(day+1)*20}/100', flush=True)
            time.sleep(2)

    def seed_reports(self):
        for i, item in enumerate(self.plan['reports']):
            key = 'report:' + item['idempotency_key']
            if key not in self.state['done']:
                _, value = self.call('incidents', '/incidencias', 'POST', item['body'],
                                     token=self.user_token(item['customer_index']),
                                     headers={'Idempotency-Key': item['idempotency_key']}, expect=(202,))
                self.record(key, value)
            if (i + 1) % 10 == 0:
                print(f'Reportes de clientes: {i+1}/100', flush=True)

    def export_accounts(self):
        with (self.directory / 'CUENTAS_DE_PRUEBA_PRIVADAS.csv').open('w', encoding='utf-8-sig', newline='') as stream:
            writer = csv.DictWriter(stream, fieldnames=['role', 'email', 'password', 'user_id'])
            writer.writeheader()
            writer.writerows({k: entry.get(k, '') for k in writer.fieldnames} for entry in self.credentials.values())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, required=True, help='Private output directory outside version control')
    parser.add_argument('--antenna-ids', type=Path, required=True, help='JSON with antenna_ids array')
    parser.add_argument('--client-id', default='7o275si2ph5vfujuj2e71f83iu')
    parser.add_argument('--admin-email', default='santiago.morales@utec.edu.pe')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--allow-admin-email', action='store_true')
    parser.add_argument('--phase', choices=['all', 'users', 'base', 'telemetry', 'reports'], default='all')
    args = parser.parse_args()
    ids = json.loads(args.antenna_ids.read_text(encoding='utf-8'))['antenna_ids']
    plan_path = args.directory / 'dataset.json'
    if plan_path.exists():
        plan = json.loads(plan_path.read_text(encoding='utf-8'))
        if [a['antenna_id'] for a in plan['antennas']] != ids:
            raise ValueError('Los IDs no coinciden con el dataset existente')
    else:
        plan = make_plan(ids, datetime.now(timezone.utc))
        save(plan_path, plan)
    print('Plan: 100 usuarios, 100 zonas, 100 estaciones, 20 antenas, 100 mediciones, 100 reportes, 30 tecnicos', flush=True)
    if not args.apply:
        print('Solo generado en disco. No se escribio en AWS.', flush=True)
        return
    if not args.allow_admin_email:
        raise ValueError('El flujo envia correos al administrador. Requiere --allow-admin-email una vez autorizado')
    password = os.environ.get('RTC_SEED_ADMIN_PASSWORD') or getpass.getpass('Clave existente del administrador: ')
    token = login(args.client_id, args.admin_email, password)
    del password
    seeder = Seeder(args.directory, plan, args.client_id, token)
    _, profile = seeder.call('identity', '/auth/perfil')
    if profile.get('user', {}).get('role') != 'admin':
        raise ValueError('La cuenta debe ser administrador')
    try:
        if args.phase in ('all', 'users'):
            seeder.seed_users()
        if args.phase in ('all', 'base'):
            seeder.seed_base()
        if args.phase in ('all', 'telemetry'):
            seeder.seed_measurements()
        if args.phase in ('all', 'reports'):
            seeder.seed_reports()
        seeder.export_accounts()
        print('Fase completada; revisar progress.json y verificar persistencia asincrona.', flush=True)
    except Exception as exc:
        seeder.state['errors'].append({'at': ISO(datetime.now(timezone.utc)), 'error': str(exc)})
        save(seeder.state_path, seeder.state)
        seeder.export_accounts()
        raise


if __name__ == '__main__':
    main()
