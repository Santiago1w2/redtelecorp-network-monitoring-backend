
import base64
import json
import logging
from decimal import Decimal, InvalidOperation

import boto3
from boto3.dynamodb.conditions import Attr
from botocore.exceptions import ClientError

# Microservicio 2: Network Infrastructure
dynamodb = boto3.resource("dynamodb")
stations_table = dynamodb.Table("redtelecorp-stations")
antennas_table = dynamodb.Table("redtelecorp-antennas")

STATES = {"ACTIVE", "INACTIVE", "MAINTENANCE"}
TECHNOLOGIES = {"3G", "4G", "5G"}

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ApiError(Exception):
    def __init__(self, status, message):
        self.status = status
        self.message = message
        super().__init__(message)


def decimal_to_json(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError("Tipo no serializable")


def reply(status, payload):
    return {
        "statusCode": status,
        "headers": {
            "Content-Type": "application/json; charset=utf-8",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "GET,POST,PATCH,OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
        },
        "body": json.dumps(payload, ensure_ascii=False, default=decimal_to_json),
    }


def read_body(event):
    body = event.get("body")
    if isinstance(body, dict):
        data = body
    else:
        if not body:
            raise ApiError(400, "Falta el cuerpo JSON")
        if event.get("isBase64Encoded"):
            try:
                body = base64.b64decode(body).decode("utf-8")
            except (ValueError, UnicodeError) as exc:
                raise ApiError(400, "Cuerpo base64 invalido") from exc
        try:
            data = json.loads(body, parse_float=Decimal)
        except (ValueError, TypeError) as exc:
            raise ApiError(400, "JSON invalido") from exc
    if not isinstance(data, dict):
        raise ApiError(400, "El JSON debe ser un objeto")
    return data


def nonempty_text(value, field, max_length=150):
    if not isinstance(value, str) or not value.strip():
        raise ApiError(400, f"{field} debe ser texto no vacio")
    cleaned = value.strip()
    if len(cleaned) > max_length:
        raise ApiError(400, f"{field} supera {max_length} caracteres")
    return cleaned


def coordinate(value, field, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ApiError(400, f"{field} debe ser numerico")
    try:
        num = Decimal(str(value))
    except InvalidOperation as exc:
        raise ApiError(400, f"{field} no es valido") from exc
    if not num.is_finite() or not (Decimal(str(minimum)) <= num <= Decimal(str(maximum))):
        raise ApiError(400, f"{field} fuera de rango [{minimum}, {maximum}]")
    return num


def one_of(value, field, allowed):
    if not isinstance(value, str) or value not in allowed:
        raise ApiError(400, f"{field} debe ser uno de: {', '.join(sorted(allowed))}")
    return value


def validate_station(data, partial=False):
    allowed = {"stationId", "name", "zoneId", "latitude", "longitude", "status"}
    editable = allowed - {"stationId"}
    unexpected = set(data) - (editable if partial else allowed)
    if unexpected:
        raise ApiError(400, "Campos no permitidos: " + ", ".join(sorted(unexpected)))
    if not partial:
        missing = {"stationId", "name", "zoneId", "latitude", "longitude"} - set(data)
        if missing:
            raise ApiError(400, "Faltan campos: " + ", ".join(sorted(missing)))
    elif not data:
        raise ApiError(400, "No hay campos para actualizar")

    result = {}
    if "stationId" in data:
        result["stationId"] = nonempty_text(data["stationId"], "stationId", 64)
    if "name" in data:
        result["name"] = nonempty_text(data["name"], "name")
    if "zoneId" in data:
        result["zoneId"] = nonempty_text(data["zoneId"], "zoneId", 64)
    if "latitude" in data:
        result["latitude"] = coordinate(data["latitude"], "latitude", -90, 90)
    if "longitude" in data:
        result["longitude"] = coordinate(data["longitude"], "longitude", -180, 180)
    if "status" in data:
        result["status"] = one_of(data["status"], "status", STATES)
    elif not partial:
        result["status"] = "ACTIVE"
    return result


def validate_antenna(data, partial=False):
    allowed = {"antennaId", "stationId", "name", "technology", "status"}
    editable = allowed - {"antennaId", "stationId"}
    unexpected = set(data) - (editable if partial else allowed)
    if unexpected:
        raise ApiError(400, "Campos no permitidos: " + ", ".join(sorted(unexpected)))
    if not partial:
        missing = {"antennaId", "stationId", "name", "technology"} - set(data)
        if missing:
            raise ApiError(400, "Faltan campos: " + ", ".join(sorted(missing)))
    elif not data:
        raise ApiError(400, "No hay campos para actualizar")

    result = {}
    for key in ("antennaId", "stationId", "name"):
        if key in data:
            result[key] = nonempty_text(data[key], key, 64 if key.endswith("Id") else 150)
    if "technology" in data:
        result["technology"] = one_of(data["technology"], "technology", TECHNOLOGIES)
    if "status" in data:
        result["status"] = one_of(data["status"], "status", STATES)
    elif not partial:
        result["status"] = "ACTIVE"
    return result


def get_or_404(table, key_name, item_id, label):
    result = table.get_item(Key={key_name: item_id})
    item = result.get("Item")
    if item is None:
        raise ApiError(404, f"{label} no encontrada")
    return item


def create_unique(table, key_name, item, label):
    try:
        table.put_item(
            Item=item,
            ConditionExpression=f"attribute_not_exists({key_name})",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(409, f"{label} ya existe") from exc
        raise
    return item


def update_existing(table, key_name, item_id, changes, label):
    names = {}
    values = {}
    updates = []
    for i, (field, value) in enumerate(changes.items()):
        names[f"#field{i}"] = field
        values[f":value{i}"] = value
        updates.append(f"#field{i} = :value{i}")

    try:
        result = table.update_item(
            Key={key_name: item_id},
            UpdateExpression="SET " + ", ".join(updates),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
            ConditionExpression=f"attribute_exists({key_name})",
            ReturnValues="ALL_NEW",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            raise ApiError(404, f"{label} no encontrada") from exc
        raise
    return result["Attributes"]


def scan_all(table, filter_expression=None):
    options = {}
    if filter_expression is not None:
        options["FilterExpression"] = filter_expression
    items = []
    while True:
        result = table.scan(**options)
        items.extend(result.get("Items", []))
        if "LastEvaluatedKey" not in result:
            return items
        options["ExclusiveStartKey"] = result["LastEvaluatedKey"]


def lambda_handler(event, context):
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "")
    path = event.get("rawPath") or event.get("path") or "/"
    parts = [part for part in path.strip("/").split("/") if part]

    try:
        if method == "OPTIONS":
            return reply(200, {"message": "OK"})

        # === ESTACIONES BASE ===
        if parts == ["stations"]:
            if method == "POST":
                station = validate_station(read_body(event))
                return reply(201, create_unique(stations_table, "stationId", station, "Estacion"))
            if method == "GET":
                items = scan_all(stations_table)
                return reply(200, {"stations": items, "count": len(items)})

        if len(parts) == 2 and parts[0] == "stations":
            station_id = parts[1]
            if method == "GET":
                return reply(200, get_or_404(stations_table, "stationId", station_id, "Estacion"))
            if method == "PATCH":
                changes = validate_station(read_body(event), partial=True)
                result = update_existing(stations_table, "stationId", station_id, changes, "Estacion")
                return reply(200, result)

        if len(parts) == 3 and parts[0] == "stations" and parts[2] == "antennas":
            if method == "GET":
                station_id = parts[1]
                get_or_404(stations_table, "stationId", station_id, "Estacion")
                items = scan_all(antennas_table, Attr("stationId").eq(station_id))
                return reply(200, {"stationId": station_id, "antennas": items, "count": len(items)})

        # === ANTENAS ===
        if parts == ["antennas"]:
            if method == "POST":
                antenna = validate_antenna(read_body(event))
                get_or_404(stations_table, "stationId", antenna["stationId"], "Estacion")
                return reply(201, create_unique(antennas_table, "antennaId", antenna, "Antena"))
            if method == "GET":
                items = scan_all(antennas_table)
                return reply(200, {"antennas": items, "count": len(items)})

        if len(parts) == 2 and parts[0] == "antennas":
            antenna_id = parts[1]
            if method == "GET":
                return reply(200, get_or_404(antennas_table, "antennaId", antenna_id, "Antena"))
            if method == "PATCH":
                changes = validate_antenna(read_body(event), partial=True)
                result = update_existing(antennas_table, "antennaId", antenna_id, changes, "Antena")
                return reply(200, result)

        return reply(404, {"error": "Ruta o metodo no encontrado"})

    except ApiError as exc:
        return reply(exc.status, {"error": exc.message})
    except ClientError:
        logger.exception("Error accediendo a DynamoDB")
        return reply(500, {"error": "No se pudo acceder a DynamoDB. Revisar logs y permisos."})
    except Exception:
        logger.exception("Error interno en Network Infrastructure")
        return reply(500, {"error": "Error interno del servidor"})
