const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, QueryCommand } = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  const { zona, triggerReporteId, usuariosUnicosAfectados, antenas_estado } = event;
  const hace24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();

  // Obtener reportes recientes de la zona
  const res = await ddb.send(
    new QueryCommand({
      TableName: process.env.REPORTES_TABLE,
      IndexName: "ZonaCreatedAtIndex",
      KeyConditionExpression: "zona = :z AND createdAt >= :desde",
      ExpressionAttributeValues: {
        ":z": zona,
        ":desde": hace24h,
      },
    })
  );

  const items = res.Items || [];
  const descripciones = items.slice(-15).map((r) => r.descripcion);
  const reporteIds = items.map((r) => r.reporteId);

  // Normalizar estado de antenas según reglas de tráfico de TeleCorp (<60 CAIDA, 0 INHABILITADA, >129 SATURADA)
  const antenasNormalizadas = (antenas_estado || []).map((ant) => {
    let estadoCalculado = "OPERATIVA";
    if (ant.trafico_gb_min === 0) estadoCalculado = "INHABILITADA";
    else if (ant.trafico_gb_min < 60) estadoCalculado = "CAIDA";
    else if (ant.trafico_gb_min > 129) estadoCalculado = "SATURADA";

    return {
      antena_id: ant.antena_id,
      trafico_gb_min: ant.trafico_gb_min,
      estado: ant.estado || estadoCalculado,
    };
  });

  // Verificar si es falla recurrente en esta zona (últimos 7 días)
  const hace7Dias = new Date(Date.now() - 7 * 24 * 60 * 60 * 1000).toISOString();
  const prevIncidentes = await ddb.send(
    new QueryCommand({
      TableName: process.env.INCIDENTES_TABLE,
      IndexName: "ZonaIncidentesIndex",
      KeyConditionExpression: "zona = :z AND createdAt >= :desde",
      ExpressionAttributeValues: {
        ":z": zona,
        ":desde": hace7Dias,
      },
    })
  );

  const historialZonaCount = (prevIncidentes.Items || []).length;

  return {
    zona,
    triggerReporteId,
    usuariosUnicosAfectados: Math.max(usuariosUnicosAfectados, new Set(items.map((i) => i.userId)).size),
    reporteIds,
    descripciones,
    antenasNormalizadas,
    esRecurrente: historialZonaCount >= 2,
    incidentesPrevios7Dias: historialZonaCount,
  };
};