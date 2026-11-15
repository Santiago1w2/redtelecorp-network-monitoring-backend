const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  UpdateCommand,
  QueryCommand,
  PutCommand,
} = require("@aws-sdk/lib-dynamodb");
const { SFNClient, StartExecutionCommand } = require("@aws-sdk/client-sfn");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const sfn = new SFNClient({});

exports.handler = async (event) => {
  for (const record of event.Records) {
    const msg = JSON.parse(record.body);
    const {
      reporteId,
      userId,
      zona,
      descripcion,
      imagen_mime_type,
      imagen_base64,
      antenas_estado,
    } = msg;

    // 1. Llamar al endpoint de IA Individual en Google Cloud
    const aiPayload = {
      usuario_id: userId,
      zona,
      descripcion,
    };
    if (imagen_mime_type && imagen_base64) {
      aiPayload.imagen_mime_type = imagen_mime_type;
      aiPayload.imagen_base64 = imagen_base64;
    }

    let aiResult = {
      categoria: "General",
      prioridad: "MEDIA",
      es_infraestructura_critica: false,
      evidencia_visual_detectada: null,
      resumen_tecnico: descripcion,
    };

    try {
      const response = await fetch(
        `${process.env.GCP_AI_BASE_URL}/api/v1/ai/prioritize`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(aiPayload),
        }
      );
      if (response.ok) {
        aiResult = await response.json();
      }
    } catch (err) {
      console.error("Fallo llamando a GCP Prioritize IA:", err);
    }

    const now = new Date().toISOString();

    // 2. Actualizar reporte con la clasificación de IA y pasarlo a EN_REVISION
    await ddb.send(
      new UpdateCommand({
        TableName: process.env.REPORTES_TABLE,
        Key: { reporteId },
        UpdateExpression:
          "set estado = :est, prioridad = :prio, categoriaIA = :cat, resumenTecnicoIA = :res, esInfraestructuraCritica = :crit, evidenciaVisual = :ev, updatedAt = :now",
        ExpressionAttributeValues: {
          ":est": "EN_REVISION",
          ":prio": aiResult.prioridad || "MEDIA",
          ":cat": aiResult.categoria || "General",
          ":res": aiResult.resumen_tecnico || descripcion,
          ":crit": Boolean(aiResult.es_infraestructura_critica),
          ":ev": aiResult.evidencia_visual_detectada || null,
          ":now": now,
        },
      })
    );

    await ddb.send(
      new PutCommand({
        TableName: process.env.HISTORIAL_TABLE,
        Item: {
          entidadId: reporteId,
          timestamp: now,
          estadoAnterior: "REGISTRADA",
          estadoNuevo: "EN_REVISION",
          actorId: "GCP-AI-PRIORITIZE",
          comentario: `Clasificado por IA: ${aiResult.categoria} | Prioridad: ${aiResult.prioridad}`,
        },
      })
    );

    // 3. Verificar regla de negocio: >= 10 usuarios distintos en la misma zona en < 24 horas
    const hace24h = new Date(Date.now() - 24 * 60 * 60 * 1000).toISOString();
    const reportesZonaRes = await ddb.send(
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

    const reportesZona = reportesZonaRes.Items || [];
    const usuariosUnicos = new Set(reportesZona.map((r) => r.userId)).size;

    const hayAntenaCaidaOSaturada = (antenas_estado || []).some(
      (a) => a.trafico_gb_min < 60 || a.trafico_gb_min > 129
    );

    // Dispara Step Function si hay >= 10 usuarios únicos, infraestructura crítica o antena afectada
    if (
      usuariosUnicos >= 10 ||
      aiResult.es_infraestructura_critica === true ||
      hayAntenaCaidaOSaturada
    ) {
      await sfn.send(
        new StartExecutionCommand({
          stateMachineArn: process.env.STATE_MACHINE_ARN,
          input: JSON.stringify({
            zona,
            triggerReporteId: reporteId,
            usuariosUnicosAfectados: usuariosUnicos,
            antenas_estado: antenas_estado || [],
          }),
        })
      );
    }
  }
};