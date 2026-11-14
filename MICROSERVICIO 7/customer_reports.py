"""Red TeleCorp - Microservicio 7: Customer Reports.

AWS Lambda + DynamoDB + EventBridge. Desplegar con serverless.yml.
No incluir secretos ni datos reales de clientes en pruebas.
"""

import base64
import binascii
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

LOG = logging.getLogger(__name__)
LOG.setLevel(logging.INFO)

REPORTS_TABLE = os.environ.get("REPORTS_TABLE", "")
EVENT_BUS = os.environ.get("EVENT_BUS", "")

# Vocabulario estable que debe compartir el microservicio 5.
PROBLEM_TYPES = {
    "NO_INTERNET", "NO_SIGNAL", "SLOW_INTERNET", "CALL_FAILURE",
    "SMS_FAILURE", "OTHER",
}
STATUSES = ("REGISTERED", "IN_REVIEW", "IN_PROGRESS", "RESOLVED", "CLOSED")
NEXT_STATUS = {
    "REGISTERED": "IN_REVIEW",
    "IN_REVIEW": "IN_PROGRESS",
    "IN_PROGRESS": "RESOLVED",
    "RESOLVED": "CLOSED",
}
EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
PHONE_PATTERN = re.compile(r"^\+?[0-9]{8,15}$")


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def now_utc():
    # ISO 8601 UTC: su orden alfabetico coincide con el cronologico.
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8"},
        "body": json.dumps(body, ensure_ascii=False),
    }


def read_json(event):
    raw = event.get("body")
    if isinstance(raw, dict):  # Facilita eventos de prueba de Lambda.
        return raw
    if not isinstance(raw, str) or not raw.strip():
        raise ApiError(400, "El cuerpo JSON es obligatorio")
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeError) as exc:
            raise ApiError(400, "El cuerpo base64 es invalido") from exc
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ApiError(400, "JSON invalido") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "Se espera un objeto JSON")
    return data


def text_field(data, field, max_length=160):
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, f"{field} es obligatorio y debe ser texto")
    value = value.strip()
    if len(value) > max_length:
        raise ApiError(400, f"{field} supera {max_length} caracteres")
    return value


def parse_timestamp(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, f"{name} requiere fecha y hora ISO 8601 con zona horaria")
    try:
        moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, f"{name} debe tener formato ISO 8601") from exc
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ApiError(400, f"{name} debe incluir zona horaria (Z o -05:00)")
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_new_report(data):
    allowed = {
        "userId", "phone", "email", "zoneId", "occurredAt",
        "problemType", "description", "locationDescription",
    }
    unknown = set(data) - allowed
    if unknown:
        raise ApiError(400, "Campos no permitidos: " + ", ".join(sorted(unknown)))

    item = {
        "userId": text_field(data, "userId", 80),
        "phone": text_field(data, "phone", 16),
        "zoneId": text_field(data, "zoneId", 80),
        "occurredAt": parse_timestamp(data.get("occurredAt"), "occurredAt"),
        "problemType": text_field(data, "problemType", 40),
        "description": text_field(data, "description", 1500),
    }
    if not PHONE_PATTERN.fullmatch(item["phone"]):
        raise ApiError(400, "phone debe contener entre 8 y 15 digitos, con + opcional")
    if item["problemType"] not in PROBLEM_TYPES:
        raise ApiError(400, "problemType invalido. Valores: " + ", ".join(sorted(PROBLEM_TYPES)))
    if "email" in data:
        email = text_field(data, "email", 254)
        if not EMAIL_PATTERN.fullmatch(email):
            raise ApiError(400, "email invalido")
        item["email"] = email
    if "locationDescription" in data:
        item["locationDescription"] = text_field(data, "locationDescription", 250)
    return item


def read_limit(query_params):
    raw = (query_params or {}).get("limit", "30")
    try:
        limit = int(raw)
    except (TypeError, ValueError) as exc:
        raise ApiError(400, "limit debe ser un entero entre 1 y 100") from exc
    if limit < 1 or limit > 100:
        raise ApiError(400, "limit debe ser un entero entre 1 y 100")
    return limit


def read_cursor(query_params):
    token = (query_params or {}).get("nextToken")
    if not token:
        return None
    try:
        if len(token) > 4000:
            raise ValueError("token largo")
        padded = token + "=" * (-len(token) % 4)
        key = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        if not isinstance(key, dict) or not key:
            raise ValueError("cursor invalido")
        return key
    except (ValueError, UnicodeError, binascii.Error, json.JSONDecodeError) as exc:
        raise ApiError(400, "nextToken invalido") from exc


def encode_cursor(key):
    if not key:
        return None
    return base64.urlsafe_b64encode(
        json.dumps(key, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")


def get_table():
    if not REPORTS_TABLE:
        raise RuntimeError("Falta REPORTS_TABLE en la configuracion de Lambda")
    return boto3.resource("dynamodb").Table(REPORTS_TABLE)


def publish_event(detail_type, data):
    """Publicacion best-effort: un error aqui no borra un reporte persistido.

    Para entrega garantizada a consumidores, evolucionar a DynamoDB Streams/outbox.
    """
    if not EVENT_BUS:
        LOG.warning("EVENT_BUS no configurado; no se publico %s", detail_type)
        return False
    try:
        result = boto3.client("events").put_events(Entries=[{
            "EventBusName": EVENT_BUS,
            "Source": "redtelecorp.customer-reports",
            "DetailType": detail_type,
            "Detail": json.dumps(data),
        }])
        if result.get("FailedEntryCount", 0):
            LOG.error("Evento rechazado por EventBridge: %s", result.get("Entries"))
            return False
        return True
    except Exception:
        LOG.exception("No se pudo publicar evento %s", detail_type)
        return False


def create_report(table, event):
    data = validate_new_report(read_json(event))
    timestamp = now_utc()
    item = {
        "reportId": "RPT-" + uuid.uuid4().hex[:16].upper(),
        **data,
        "reportedAt": timestamp,
        "updatedAt": timestamp,
        "status": "REGISTERED",
        "statusHistory": [{"status": "REGISTERED", "changedAt": timestamp}],
    }
    table.put_item(Item=item, ConditionExpression="attribute_not_exists(reportId)")
    published = publish_event("CustomerReportCreated", {
        "reportId": item["reportId"],
        "userId": item["userId"],
        "zoneId": item["zoneId"],
        "problemType": item["problemType"],
        "reportedAt": item["reportedAt"],
    })
    return response(201, {"report": item, "eventPublished": published})


def get_report(table, report_id):
    result = table.get_item(Key={"reportId": report_id}, ConsistentRead=True)
    item = result.get("Item")
    if item is None:
        raise ApiError(404, "Reporte no encontrado")
    return item


def list_reports(table, event, index=None, id_field=None, id_value=None):
    query = event.get("queryStringParameters") or {}
    params = {"Limit": read_limit(query)}
    cursor = read_cursor(query)
    if cursor:
        params["ExclusiveStartKey"] = cursor

    if index:
        params["IndexName"] = index
        condition = Key(id_field).eq(id_value)
        if query.get("since"):
            since = parse_timestamp(query["since"], "since")
            condition = condition & Key("reportedAt").gte(since)
        params["KeyConditionExpression"] = condition
        params["ScanIndexForward"] = False
        result = table.query(**params)
    else:
        if query.get("since"):
            raise ApiError(400, "since solo funciona con /reports/user/{userId} o /reports/zone/{zoneId}")
        result = table.scan(**params)

    return response(200, {
        "reports": result.get("Items", []),
        "count": len(result.get("Items", [])),
        "nextToken": encode_cursor(result.get("LastEvaluatedKey")),
    })


def change_report_status(table, report_id, event):
    body = read_json(event)
    if set(body) - {"status", "note"}:
        raise ApiError(400, "Solo se permiten status y note")
    new_status = text_field(body, "status", 30)
    if new_status not in STATUSES:
        raise ApiError(400, "status invalido. Valores: " + ", ".join(STATUSES))
    note = None
    if "note" in body:
        note = text_field(body, "note", 300)

    current = get_report(table, report_id)
    old_status = current["status"]
    if NEXT_STATUS.get(old_status) != new_status:
        raise ApiError(409, f"Transicion no permitida: {old_status} -> {new_status}")

    timestamp = now_utc()
    history_entry = {"status": new_status, "changedAt": timestamp}
    if note:
        history_entry["note"] = note
    try:
        result = table.update_item(
            Key={"reportId": report_id},
            UpdateExpression=(
                "SET #st = :new, updatedAt = :now, "
                "statusHistory = list_append(if_not_exists(statusHistory, :empty), :history)"
            ),
            ConditionExpression="attribute_exists(reportId) AND #st = :old",
            ExpressionAttributeNames={"#st": "status"},
            ExpressionAttributeValues={
                ":new": new_status,
                ":old": old_status,
                ":now": timestamp,
                ":history": [history_entry],
                ":empty": [],
            },
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise ApiError(409, "El reporte fue modificado por otra solicitud; vuelve a consultar") from exc
        raise
    updated = result["Attributes"]
    published = publish_event("CustomerReportStatusChanged", {
        "reportId": report_id,
        "zoneId": updated["zoneId"],
        "previousStatus": old_status,
        "status": new_status,
        "updatedAt": timestamp,
    })
    return response(200, {"report": updated, "eventPublished": published})


def lambda_handler(event, context):
    method = event.get("httpMethod") or (event.get("requestContext", {}).get("http", {}).get("method"))
    path = event.get("rawPath") or event.get("path") or "/"
    parts = [piece for piece in path.strip("/").split("/") if piece]

    try:
        table = get_table()
        if method == "POST" and parts == ["reports"]:
            return create_report(table, event)
        if method == "GET" and parts == ["reports"]:
            return list_reports(table, event)
        if method == "GET" and len(parts) == 3 and parts[:2] == ["reports", "user"]:
            return list_reports(table, event, "userId-reportedAt-index", "userId", parts[2])
        if method == "GET" and len(parts) == 3 and parts[:2] == ["reports", "zone"]:
            return list_reports(table, event, "zoneId-reportedAt-index", "zoneId", parts[2])
        if len(parts) == 2 and parts[0] == "reports":
            if method == "GET":
                return response(200, {"report": get_report(table, parts[1])})
        if method == "PATCH" and len(parts) == 3 and parts[0] == "reports" and parts[2] == "status":
            return change_report_status(table, parts[1], event)
        return response(404, {"error": "Ruta o metodo no encontrado"})
    except ApiError as exc:
        return response(exc.status, {"error": exc.message})
    except ClientError:
        LOG.exception("Error de AWS en Customer Reports")
        return response(500, {"error": "Error accediendo a los servicios de AWS"})
    except Exception:
        LOG.exception("Error interno en Customer Reports")
        return response(500, {"error": "Error interno del servidor"})
