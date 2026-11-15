const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const {
  DynamoDBDocumentClient,
  UpdateCommand,
  PutCommand,
} = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  const { incidenteId, zona, diagnosticoIA, reporteIds } = event;
  const now = new Date().toISOString();

  // Escalar incidente colectivo a EN_ATENCION para técnico de campo
  await ddb.send(
    new UpdateCommand({
      TableName: process.env.INCIDENTES_TABLE,
      Key: { incidenteId },
      UpdateExpression: "set estado = :est, cuadrillaAsignada = :cuad, updatedAt = :now",
      ExpressionAttributeValues: {
        ":est": "EN_ATENCION",
        ":cuad": true,
        ":now": now,
      },
    })
  );

  await ddb.send(
    new PutCommand({
      TableName: process.env.HISTORIAL_TABLE,
      Item: {
        entidadId: incidenteId,
        timestamp: now,
        estadoAnterior: "DETECTADO",
        estadoNuevo: "EN_ATENCION",
        actorId: "AWS-STEP-FUNCTIONS",
        comentario: `Cuadrilla despachada automáticamente a ${zona}. Notificación emitida a ${reporteIds.length} reportes.`,
      },
    })
  );

  // Actualizar los reportes individuales vinculados a SIENDO_ATENDIDA
  for (const rId of reporteIds) {
    await ddb.send(
      new UpdateCommand({
        TableName: process.env.REPORTES_TABLE,
        Key: { reporteId: rId },
        UpdateExpression: "set estado = :est, mensajeUsuario = :msg, updatedAt = :now",
        ExpressionAttributeValues: {
          ":est": "SIENDO_ATENDIDA",
          ":msg": diagnosticoIA.mensaje_notificacion_usuario,
          ":now": now,
        },
      })
    );
  }

  return {
    status: "DESPACHADO_Y_NOTIFICADO",
    incidenteId,
    zona,
    mensajeEnviado: diagnosticoIA.mensaje_notificacion_usuario,
  };
};