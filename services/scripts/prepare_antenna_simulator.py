"""Register 20 stable random demo antennas through the real Infrastructure API."""
import argparse
import getpass
import json
import os
import re
from pathlib import Path
import sys
from urllib import error, parse, request
import uuid


def http(url, method='GET', payload=None, headers=None):
    req = request.Request(url, data=json.dumps(payload).encode() if payload is not None else None,
                          headers={'Content-Type': 'application/json', **(headers or {})}, method=method)
    try:
        with request.urlopen(req, timeout=20) as response:
            return response.status, json.loads(response.read(1000000))
    except error.HTTPError as exc:
        return exc.code, {}


def manifest(path, base, zone, latitude, longitude):
    config = {'infrastructure_url': base, 'zone_id': zone, 'latitude': latitude, 'longitude': longitude}
    if path.exists():
        saved = json.loads(path.read_text(encoding='utf-8'))
        if any(saved.get(key) != value for key, value in config.items()):
            raise ValueError('Manifest belongs to another configuration; use another --manifest path')
        ids = saved.get('antenna_ids', [])
        if len(ids) != 20 or len(set(ids)) != 20 or any(not re.fullmatch(r'ANT-SIM-[a-f0-9]{32}', item) for item in ids):
            raise ValueError('Invalid antenna manifest')
        return saved
    saved = {**config, 'station_id': 'STA-SIM-' + uuid.uuid4().hex,
             'antenna_ids': ['ANT-SIM-' + uuid.uuid4().hex for _ in range(20)]}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(saved, indent=2) + '\n', encoding='utf-8')
    return saved


def register(base, route, item, key, headers):
    code, _ = http(base + route, 'POST', item, headers)
    if code == 409:
        code, found = http(base + route + '/' + item[key], headers=headers)
        if code != 200 or any(found.get(k) != value for k, value in item.items()):
            raise RuntimeError('Existing resource differs from demo manifest: ' + item[key])
    elif code != 201:
        raise RuntimeError(f'Registration failed: {route}, HTTP {code}; rerun with the same manifest')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--infrastructure-url', required=True)
    parser.add_argument('--zone-id', required=True, help='Existing demo zone')
    parser.add_argument('--latitude', required=True, type=float)
    parser.add_argument('--longitude', required=True, type=float)
    parser.add_argument('--client-id', default=os.environ.get('COGNITO_CLIENT_ID', ''))
    parser.add_argument('--username', default='')
    parser.add_argument('--region', default='us-east-1')
    parser.add_argument('--manifest', type=Path, default=Path('.antenna-simulator/manifest.json'))
    args = parser.parse_args()
    base = args.infrastructure_url.rstrip('/')
    url = parse.urlsplit(base)
    if url.scheme != 'https' or not url.hostname or url.username or url.query or url.fragment:
        parser.error('Use the HTTPS Infrastructure API base URL')
    if not -90 <= args.latitude <= 90 or not -180 <= args.longitude <= 180:
        parser.error('Coordinates out of range')
    token = os.environ.get('COGNITO_ID_TOKEN')
    if not token:
        if not args.client_id:
            parser.error('Set --client-id or COGNITO_ID_TOKEN')
        username = args.username or input('Usuario administrador de Cognito: ').strip()
        password = getpass.getpass('Contrasena (entrada oculta, no se guarda): ')
        code, auth = http('https://cognito-idp.' + args.region + '.amazonaws.com/', 'POST',
                          {'AuthFlow': 'USER_PASSWORD_AUTH', 'ClientId': args.client_id,
                           'AuthParameters': {'USERNAME': username, 'PASSWORD': password}},
                          {'Content-Type': 'application/x-amz-json-1.1',
                           'X-Amz-Target': 'AWSCognitoIdentityProviderService.InitiateAuth'})
        del password
        token = auth.get('AuthenticationResult', {}).get('IdToken')
        if code != 200 or not token:
            raise RuntimeError('Cognito login not completed; complete any challenge separately')
    saved = manifest(args.manifest, base, args.zone_id, args.latitude, args.longitude)
    headers = {'Authorization': 'Bearer ' + token}
    station = {'stationId': saved['station_id'], 'name': 'Estacion simulada de pruebas',
               'zoneId': args.zone_id, 'latitude': args.latitude, 'longitude': args.longitude}
    register(base, '/stations', station, 'stationId', headers)
    for index, antenna in enumerate(saved['antenna_ids'], 1):
        register(base, '/antennas', {'antenna_id': antenna, 'stationId': saved['station_id'],
                                   'name': f'Antena simulada {index:02d}', 'technology': '4G'}, 'antenna_id', headers)
        print(f'Antena {index}/20 registrada')
    config = args.manifest.parent / 'simulator.env'
    config.write_text("SIMULATOR_ENABLED=true\nSIMULATOR_SAMPLE_SIZE=5\nSIMULATOR_ANTENNA_IDS='" +
                      ','.join(saved['antenna_ids']) + "'\n", encoding='utf-8', newline='\n')
    print('Preparacion completada. Conservar el manifest para reutilizar los mismos IDs.')
    print('Agregar al deploy_all.env:')
    print('source ' + repr(config.resolve().as_posix()))


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, ValueError, error.URLError) as exc:
        print('ERROR:', str(exc), file=sys.stderr)
        sys.exit(1)
