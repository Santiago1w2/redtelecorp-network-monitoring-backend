"""Red TeleCorp - Microservicio 8: Field Operations.

AWS Lambda + DynamoDB + EventBridge.
- Tecnicos, ordenes de trabajo y asignacion automatica por zona y especialidad.
- Reserva atomica de un tecnico y creacion de la orden usando TransactWriteItems.
- Evento opcional de Incident Management: IncidentNeedsTechnician.
- API HTTP protegida con Cognito; eventos internos recibidos desde EventBridge.
"""
import base64
import binascii
import hashlib
import json
import logging
import os
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
LOGGER.setLevel(logging.INFO)
SERIALIZER = TypeSerializer()

TECHNICIANS_TABLE = os.environ.get('TECHNICIANS_TABLE', '')
WORK_ORDERS_TABLE = os.environ.get('WORK_ORDERS_TABLE', '')
EVENT_BUS = os.environ.get('EVENT_BUS', 'default')

TECH_STATUSES = {'AVAILABLE', 'BUSY', 'INACTIVE'}
ORDER_STATUSES = {'PENDING_ASSIGNMENT', 'ASSIGNED', 'IN_PROGRESS', 'COMPLETED', 'CANCELLED'}
PRIORITIES = {'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'}
TRANSITIONS = {
    'PENDING_ASSIGNMENT': {'CANCELLED'},
    'ASSIGNED': {'IN_PROGRESS', 'COMPLETED', 'CANCELLED'},
    'IN_PROGRESS': {'COMPLETED', 'CANCELLED'},
    'COMPLETED': set(),
    'CANCELLED': set(),
}


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec='microseconds').replace('+00:00', 'Z')


def response(status, payload):
    return {
        'statusCode': status,
        'headers': {'Content-Type': 'application/json; charset=utf-8'},
        'body': json.dumps(payload, ensure_ascii=False),
    }


def parse_body(event):
    raw = event.get('body')
    if isinstance(raw, dict):
        return raw  # conveniente para test de Lambda
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, 'El cuerpo JSON es obligatorio')
    if event.get('isBase64Encoded'):
        try:
            raw = base64.b64decode(raw, validate=True).decode('utf-8')
        except (ValueError, UnicodeError, binascii.Error) as exc:
            raise ApiError(400, 'Cuerpo base64 invalido') from exc
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise ApiError(400, 'JSON invalido') from exc
    if not isinstance(data, dict):
        raise ApiError(400, 'Se espera un objeto JSON')
    return data


def text_field(data, name, max_length=150, required=True):
    value = data.get(name)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, f'{name} debe ser texto no vacio')
    value = value.strip()
    if len(value) > max_length:
        raise ApiError(400, f'{name} supera {max_length} caracteres')
    return value


def enum_field(data, name, allowed, default=None):
    value = data.get(name, default)
    if not isinstance(value, str) or value not in allowed:
        raise ApiError(400, f'{name} debe ser: {", ".join(sorted(allowed))}')
    return value


def skills_field(data):
    skills = data.get('skills')
    if not isinstance(skills, list) or not skills or len(skills) > 15:
        raise ApiError(400, 'skills debe ser una lista no vacia (maximo 15)')
    cleaned = [text_field({'skill': s}, 'skill', 40).upper() for s in skills]
    if len(set(cleaned)) != len(cleaned):
        raise ApiError(400, 'skills no puede tener duplicados')
    return cleaned


def reject_unknown(data, allowed):
    extra = set(data) - allowed
    if extra:
        raise ApiError(400, 'Campos no permitidos: ' + ', '.join(sorted(extra)))


def validate_technician(data, partial=False):
    allowed = {'technicianId', 'name', 'zoneId', 'skills', 'status'}
    reject_unknown(data, allowed - ({'technicianId'} if partial else set()))
    if not partial:
        required = {'technicianId', 'name', 'zoneId', 'skills'}
        missing = required - set(data)
        if missing:
            raise ApiError(400, 'Faltan campos: ' + ', '.join(sorted(missing)))
    elif not data:
        raise ApiError(400, 'No hay campos que actualizar')
    result = {}
    for name in ('technicianId', 'name', 'zoneId'):
        if name in data:
            result[name] = text_field(data, name, 64 if name.endswith('Id') else 150)
    if 'skills' in data:
        result['skills'] = skills_field(data)
    if 'status' in data or not partial:
        status = enum_field(data, 'status', {'AVAILABLE', 'INACTIVE'}, 'AVAILABLE')
        result['status'] = status
    return result


def validate_order(data):
    allowed = {'incidentId', 'zoneId', 'requiredSkill', 'description', 'priority', 'antennaId'}
    reject_unknown(data, allowed)
    result = {
        'incidentId': text_field(data, 'incidentId', 100),
        'zoneId': text_field(data, 'zoneId', 64),
        'requiredSkill': text_field(data, 'requiredSkill', 40).upper(),
        'description': text_field(data, 'description', 1000),
        'priority': enum_field(data, 'priority', PRIORITIES, 'MEDIUM'),
    }
    if 'antennaId' in data:
        result['antennaId'] = text_field(data, 'antennaId', 64)
    return result


def table(name):
    if not name:
        raise RuntimeError('Falta nombre de tabla DynamoDB en environment')
    return boto3.resource('dynamodb').Table(name)


def marshal(value):
    return {key: SERIALIZER.serialize(val) for key, val in value.items()}


def get_or_404(db_table, key, item_id, name):
    item = db_table.get_item(Key={key: item_id}, ConsistentRead=True).get('Item')
    if item is None:
        raise ApiError(404, f'{name} no encontrado')
    return item


def scan_page(db_table, event):
    query = event.get('queryStringParameters') or {}
    try:
        limit = int(query.get('limit', '30'))
        if not 1 <= limit <= 100:
            raise ValueError
    except (ValueError, TypeError) as exc:
        raise ApiError(400, 'limit debe ser entero entre 1 y 100') from exc
    params = {'Limit': limit}
    if query.get('nextToken'):
        try:
            token = query['nextToken']
            if len(token) > 4000:
                raise ValueError
            padded = token + '=' * (-len(token) % 4)
            key = json.loads(base64.urlsafe_b64decode(padded).decode('utf-8'))
            if not isinstance(key, dict) or not key:
                raise ValueError
            params['ExclusiveStartKey'] = key
        except (ValueError, UnicodeError, binascii.Error, TypeError) as exc:
            raise ApiError(400, 'nextToken invalido') from exc
    result = db_table.scan(**params)
    next_key = result.get('LastEvaluatedKey')
    token = (base64.urlsafe_b64encode(json.dumps(next_key).encode()).decode().rstrip('=')
             if next_key else None)
    return {'items': result.get('Items', []), 'nextToken': token}


def publish(detail_type, order):
    detail = {key: order[key] for key in (
        'workOrderId', 'incidentId', 'zoneId', 'status', 'assignedTechnicianId'
    ) if key in order}
    try:
        result = boto3.client('events').put_events(Entries=[{
            'Source': 'redtelecorp.field-operations',
            'DetailType': detail_type,
            'Detail': json.dumps(detail),
            'EventBusName': EVENT_BUS,
        }])
        if result.get('FailedEntryCount', 0):
            raise RuntimeError(f'EventBridge no publicó {detail_type}')
    except Exception:
        LOGGER.exception('No se pudo publicar el evento %s', detail_type)
        raise


def candidates(zone_id, required_skill):
    """Toma tecnicos de la zona, disponibles y con especialidad; orden por ID."""
    db_table = table(TECHNICIANS_TABLE)
    params = {
        'IndexName': 'zoneId-index',
        'KeyConditionExpression': Key('zoneId').eq(zone_id),
    }
    found = []
    while True:
        result = db_table.query(**params)
        for tech in result.get('Items', []):
            if tech.get('status') == 'AVAILABLE' and required_skill in tech.get('skills', []):
                found.append(tech)
        if 'LastEvaluatedKey' not in result:
            break
        params['ExclusiveStartKey'] = result['LastEvaluatedKey']
    return sorted(found, key=lambda tech: tech['technicianId'])


def create_technician(data):
    item = validate_technician(data)
    item['createdAt'] = now_utc()
    item['updatedAt'] = item['createdAt']
    try:
        table(TECHNICIANS_TABLE).put_item(
            Item=item, ConditionExpression='attribute_not_exists(technicianId)'
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ApiError(409, 'Tecnico ya existe') from exc
        raise
    return response(201, item)


def update_technician(technician_id, data):
    changes = validate_technician(data, partial=True)
    changes['updatedAt'] = now_utc()
    names = {'#status': 'status'}
    values = {':busy': 'BUSY'}
    set_parts = []
    for i, (field, value) in enumerate(changes.items()):
        names[f'#f{i}'] = field
        values[f':v{i}'] = value
        set_parts.append(f'#f{i} = :v{i}')
    try:
        result = table(TECHNICIANS_TABLE).update_item(
            Key={'technicianId': technician_id},
            UpdateExpression='SET ' + ', '.join(set_parts),
            ConditionExpression='attribute_exists(technicianId) AND #status <> :busy',
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ReturnValues='ALL_NEW',
        )
    except ClientError as exc:
        if exc.response['Error']['Code'] == 'ConditionalCheckFailedException':
            raise ApiError(409, 'Tecnico inexistente u ocupado; no se puede editar') from exc
        raise
    return response(200, result['Attributes'])


def order_id_from_incident(incident_id):
    # Evita crear dos ordenes por la misma incidencia al reintentar un evento.
    return 'WO-' + hashlib.sha256(incident_id.encode('utf-8')).hexdigest()[:24].upper()


def create_order(data):
    validated = validate_order(data)
    order_id = order_id_from_incident(validated['incidentId'])
    orders = table(WORK_ORDERS_TABLE)
    existing = orders.get_item(Key={'workOrderId': order_id}, ConsistentRead=True).get('Item')
    if existing:
        if existing['incidentId'] != validated['incidentId']:
            raise ApiError(409, 'Conflicto de identificador de orden')
        publish('WorkOrderPending' if existing['status'] == 'PENDING_ASSIGNMENT' else 'WorkOrderAssigned', existing)
        return response(200, existing)  # idempotencia ante reintentos

    timestamp = now_utc()
    base = {
        'workOrderId': order_id,
        **validated,
        'createdAt': timestamp,
        'updatedAt': timestamp,
    }
    for tech in candidates(validated['zoneId'], validated['requiredSkill']):
        assigned = {
            **base, 'status': 'ASSIGNED',
            'assignedTechnicianId': tech['technicianId'],
            'statusHistory': [{'status': 'ASSIGNED', 'changedAt': timestamp}],
        }
        try:
            boto3.client('dynamodb').transact_write_items(TransactItems=[
                {'Update': {
                    'TableName': TECHNICIANS_TABLE,
                    'Key': marshal({'technicianId': tech['technicianId']}),
                    'UpdateExpression': 'SET #s = :busy, assignedWorkOrderId = :oid, updatedAt = :t',
                    'ConditionExpression': '#s = :available AND attribute_not_exists(assignedWorkOrderId)',
                    'ExpressionAttributeNames': {'#s': 'status'},
                    'ExpressionAttributeValues': marshal({
                        ':busy': 'BUSY', ':available': 'AVAILABLE',
                        ':oid': order_id, ':t': timestamp,
                    }),
                }},
                {'Put': {
                    'TableName': WORK_ORDERS_TABLE,
                    'Item': marshal(assigned),
                    'ConditionExpression': 'attribute_not_exists(workOrderId)',
                }},
            ])
            publish('WorkOrderAssigned', assigned)
            return response(201, assigned)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in {'TransactionCanceledException', 'TransactionConflictException'}:
                raise
            existing = orders.get_item(Key={'workOrderId': order_id}, ConsistentRead=True).get('Item')
            if existing:
                return response(200, existing)
            # Puede haberse ocupado el tecnico. Probar el siguiente.
            LOGGER.info('No fue posible reservar tecnico %s', tech['technicianId'])

    pending = {
        **base,
        'status': 'PENDING_ASSIGNMENT',
        'statusHistory': [{'status': 'PENDING_ASSIGNMENT', 'changedAt': timestamp}],
    }
    try:
        orders.put_item(Item=pending, ConditionExpression='attribute_not_exists(workOrderId)')
    except ClientError as exc:
        if exc.response['Error']['Code'] != 'ConditionalCheckFailedException':
            raise
        return response(200, get_or_404(orders, 'workOrderId', order_id, 'Orden'))
    publish('WorkOrderPending', pending)
    return response(201, pending)


def assign_pending(work_order_id):
    orders = table(WORK_ORDERS_TABLE)
    order = get_or_404(orders, 'workOrderId', work_order_id, 'Orden')
    if order['status'] != 'PENDING_ASSIGNMENT':
        if order['status'] == 'ASSIGNED':
            publish('WorkOrderAssigned', order)
            return response(200, order)
        raise ApiError(409, 'Solo se pueden asignar ordenes pendientes')
    timestamp = now_utc()
    for tech in candidates(order['zoneId'], order['requiredSkill']):
        try:
            boto3.client('dynamodb').transact_write_items(TransactItems=[
                {'Update': {
                    'TableName': TECHNICIANS_TABLE,
                    'Key': marshal({'technicianId': tech['technicianId']}),
                    'UpdateExpression': 'SET #s = :busy, assignedWorkOrderId = :oid, updatedAt = :t',
                    'ConditionExpression': '#s = :available AND attribute_not_exists(assignedWorkOrderId)',
                    'ExpressionAttributeNames': {'#s': 'status'},
                    'ExpressionAttributeValues': marshal({
                        ':busy': 'BUSY', ':available': 'AVAILABLE',
                        ':oid': work_order_id, ':t': timestamp,
                    }),
                }},
                {'Update': {
                    'TableName': WORK_ORDERS_TABLE,
                    'Key': marshal({'workOrderId': work_order_id}),
                    'UpdateExpression': 'SET #s = :assigned, assignedTechnicianId = :tid, updatedAt = :t, statusHistory = list_append(statusHistory, :history)',
                    'ConditionExpression': '#s = :pending',
                    'ExpressionAttributeNames': {'#s': 'status'},
                    'ExpressionAttributeValues': marshal({
                        ':assigned': 'ASSIGNED', ':pending': 'PENDING_ASSIGNMENT',
                        ':tid': tech['technicianId'], ':t': timestamp,
                        ':history': [{'status': 'ASSIGNED', 'changedAt': timestamp}],
                    }),
                }},
            ])
            updated = get_or_404(orders, 'workOrderId', work_order_id, 'Orden')
            publish('WorkOrderAssigned', updated)
            return response(200, updated)
        except ClientError as exc:
            if exc.response['Error']['Code'] not in {'TransactionCanceledException', 'TransactionConflictException'}:
                raise
            current = get_or_404(orders, 'workOrderId', work_order_id, 'Orden')
            if current['status'] != 'PENDING_ASSIGNMENT':
                raise ApiError(409, 'La orden ya fue asignada por otra solicitud') from exc
    return response(409, {'error': 'No hay tecnicos disponibles con esa especialidad en la zona'})


def change_order_status(work_order_id, data):
    reject_unknown(data, {'status'})
    new_status = enum_field(data, 'status', ORDER_STATUSES)
    orders = table(WORK_ORDERS_TABLE)
    order = get_or_404(orders, 'workOrderId', work_order_id, 'Orden')
    old_status = order['status']
    if old_status == new_status:
        publish('WorkOrderStatusChanged', order)
        return response(200, order)
    if new_status not in TRANSITIONS[old_status]:
        raise ApiError(409, f'Transicion no permitida: {old_status} -> {new_status}')
    now = now_utc()
    update = {
        'TableName': WORK_ORDERS_TABLE,
        'Key': marshal({'workOrderId': work_order_id}),
        'UpdateExpression': 'SET #s = :new, updatedAt = :t, statusHistory = list_append(statusHistory, :history)',
        'ConditionExpression': '#s = :old',
        'ExpressionAttributeNames': {'#s': 'status'},
        'ExpressionAttributeValues': marshal({
            ':new': new_status, ':old': old_status, ':t': now,
            ':history': [{'status': new_status, 'changedAt': now}],
        }),
    }
    actions = [{'Update': update}]
    if new_status in {'COMPLETED', 'CANCELLED'} and order.get('assignedTechnicianId'):
        actions.append({'Update': {
            'TableName': TECHNICIANS_TABLE,
            'Key': marshal({'technicianId': order['assignedTechnicianId']}),
            'UpdateExpression': 'SET #s = :available, updatedAt = :t REMOVE assignedWorkOrderId',
            'ConditionExpression': '#s = :busy AND assignedWorkOrderId = :oid',
            'ExpressionAttributeNames': {'#s': 'status'},
            'ExpressionAttributeValues': marshal({
                ':available': 'AVAILABLE', ':busy': 'BUSY',
                ':t': now, ':oid': work_order_id,
            }),
        }})
    try:
        boto3.client('dynamodb').transact_write_items(TransactItems=actions)
    except ClientError as exc:
        if exc.response['Error']['Code'] in {'TransactionCanceledException', 'TransactionConflictException'}:
            raise ApiError(409, 'Conflicto al actualizar; vuelve a consultar la orden') from exc
        raise
    updated = get_or_404(orders, 'workOrderId', work_order_id, 'Orden')
    publish('WorkOrderStatusChanged', updated)
    return response(200, updated)


def lambda_handler(event, context):
    try:
        # Evento publicado por Incident Management en EventBridge (bus default).
        if event.get('source') == 'redtelecorp.incident-management' and event.get('detail-type') == 'IncidentNeedsTechnician':
            result = create_order(event.get('detail') or {})
            return {'handled': True, 'statusCode': result['statusCode']}

        claims = ((event.get('requestContext') or {}).get('authorizer') or {}).get('jwt', {}).get('claims') or {}
        if not claims:
            claims = ((event.get('requestContext') or {}).get('authorizer') or {}).get('claims') or {}
        raw_groups = claims.get('cognito:groups', [])
        if isinstance(raw_groups, str):
            try:
                raw_groups = json.loads(raw_groups)
            except ValueError:
                raw_groups = raw_groups.replace(',', ' ').split()
        allowed_groups = {'admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo'}
        if not isinstance(raw_groups, list) or not allowed_groups.intersection(raw_groups):
            return response(403, {'error': 'Se requiere un rol interno de Red TeleCorp'})

        method = event.get('httpMethod') or (event.get('requestContext') or {}).get('http', {}).get('method')
        path = event.get('rawPath') or event.get('path') or '/'
        segments = [p for p in path.strip('/').split('/') if p]
        if segments == ['technicians']:
            if method == 'POST':
                return create_technician(parse_body(event))
            if method == 'GET':
                return response(200, {'technicians': scan_page(table(TECHNICIANS_TABLE), event)})
        if len(segments) == 2 and segments[0] == 'technicians':
            if method == 'GET':
                return response(200, get_or_404(table(TECHNICIANS_TABLE), 'technicianId', segments[1], 'Tecnico'))
            if method == 'PATCH':
                return update_technician(segments[1], parse_body(event))
        if segments == ['work-orders']:
            if method == 'POST':
                return create_order(parse_body(event))
            if method == 'GET':
                return response(200, {'workOrders': scan_page(table(WORK_ORDERS_TABLE), event)})
        if len(segments) == 2 and segments[0] == 'work-orders' and method == 'GET':
            return response(200, get_or_404(table(WORK_ORDERS_TABLE), 'workOrderId', segments[1], 'Orden'))
        if len(segments) == 3 and segments[0] == 'work-orders':
            if segments[2] == 'assign' and method == 'POST':
                return assign_pending(segments[1])
            if segments[2] == 'status' and method == 'PATCH':
                return change_order_status(segments[1], parse_body(event))
        return response(404, {'error': 'Ruta o metodo no encontrado'})
    except ApiError as exc:
        return response(exc.status, {'error': exc.message})
    except ClientError:
        LOGGER.exception('Error AWS DynamoDB/EventBridge')
        return response(500, {'error': 'Error de infraestructura AWS'})
    except Exception:
        LOGGER.exception('Error interno Field Operations')
        return response(500, {'error': 'Error interno del servidor'})
