const crypto = require("crypto");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  PutCommand,
  UpdateCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  const {
    zona,
    usuariosUnicosAfectados,
    reporteIds,
    antenasNormalizadas,
    esRecurrente,
    diagnosticoIA,
  } = event;

  const incidenteId = `INC-${crypto.randomUUID()}`;
  const now = new Date().toISOString();

  const incidente = {
    incidenteId,
    zona,
    usuariosAfectados: usuariosUnicosAfectados,
    totalReportesVinculados: reporteIds.length,
    reporteIds,
    antenasAfectadas: antenasNormalizadas,
    esRecurrente,
    estado: "DETECTADO",
    prioridad: diagnosticoIA.riesgo_estructural || "ALTA",
    causaRaizProbable: diagnosticoIA.causa_raiz_probable,
    requiereCuadrillaCampo: diagnosticoIA.requiere_cuadrilla_campo,
    mensajeNotificacionUsuario: diagnosticoIA.mensaje_notificacion_usuario,
    createdAt: now,
    updatedAt: now,
  };

  // 1. Guardar Incidente Colectivo en DynamoDB
  await ddb.send(
    new PutCommand({
      TableName: process.env.INCIDENTES_TABLE,
      Item: incidente,
    })
  );

  // 2. Registrar en historial de auditoría
  await ddb.send(
    new PutCommand({
      TableName: process.env.HISTORIAL_TABLE,
      Item: {
        entidadId: incidenteId,
        timestamp: now,
        estadoAnterior: null,
        estadoNuevo: "DETECTADO",
        actorId: "AWS-STEP-FUNCTIONS",
        comentario: `Incidente colectivo detectado (${usuariosUnicosAfectados} usuarios). Diagnóstico IA: ${diagnosticoIA.causa_raiz_probable}`,
      },
    })
  );

  // 3. Vincular todos los reportes individuales al incidente colectivo
  for (const rId of reporteIds) {
    await ddb.send(
      new UpdateCommand({
        TableName: process.env.REPORTES_TABLE,
        Key: { reporteId: rId },
        UpdateExpression: "set incidenteColectivoId = :inc, updatedAt = :now",
        ExpressionAttributeValues: {
          ":inc": incidenteId,
          ":now": now,
        },
      })
    );
  }

  return {
    ...event,
    incidenteId,
    incidente,
  };
};