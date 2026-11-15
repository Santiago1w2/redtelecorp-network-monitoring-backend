const crypto = require("crypto");
const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, PutCommand } = require("@aws-sdk/lib-dynamodb");
const { SQSClient, SendMessageCommand } = require("@aws-sdk/client-sqs");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));
const sqs = new SQSClient({});

exports.handler = async (event) => {
  try {
    const claims = event.requestContext.authorizer?.claims || {};
    const userId = claims.sub;
    if (!userId) return { statusCode: 401, headers: { "Access-Control-Allow-Origin": "*" }, body: JSON.stringify({ message: "Token Cognito inválido" }) };
    const body = JSON.parse(event.body || "{}");

    const {
      numeroServicio,
      zona,
      tipoProblema,
      descripcion,
      fechaIncidente,
      imagen_mime_type,
      imagen_base64,
    } = body;

    if (!zona || !descripcion || !numeroServicio) {
      return {
        statusCode: 400,
        headers: { "Access-Control-Allow-Origin": "*" },
        body: JSON.stringify({
          message: "Los campos numeroServicio, zona y descripcion son obligatorios",
        }),
      };
    }

    const reporteId = `REP-${crypto.randomUUID()}`;
    const now = new Date().toISOString();

    const item = {
      reporteId,
      userId,
      emailUsuario: claims.email || null,
      dniUsuario: claims["custom:dni"] || null,
      numeroServicio,
      zona: zona.trim(),
      tipoProblema: tipoProblema || "GENERAL",
      descripcion,
      fechaIncidente: fechaIncidente || now,
      estado: "REGISTRADA",
      statusHistory: [{ estadoAnterior: null, estadoNuevo: "REGISTRADA", timestamp: now, actorId: userId }],
      prioridad: "PENDIENTE_IA",
      incidenteColectivoId: null,
      createdAt: now,
      updatedAt: now,
    };

    // 1. Guardar reporte inicial en DynamoDB
    await ddb.send(
      new PutCommand({
        TableName: process.env.REPORTES_TABLE,
        Item: item,
      })
    );

    // 2. Registrar auditoría inicial
    await ddb.send(
      new PutCommand({
        TableName: process.env.HISTORIAL_TABLE,
        Item: {
          entidadId: reporteId,
          timestamp: now,
          estadoAnterior: null,
          estadoNuevo: "REGISTRADA",
          actorId: userId,
          comentario: "Reporte registrado por el usuario",
        },
      })
    );

    // 3. Encolar en SQS para procesamiento asíncrono con IA de GCP (incluye imagen si existe)
    await sqs.send(
      new SendMessageCommand({
        QueueUrl: process.env.REPORTES_QUEUE_URL,
        MessageBody: JSON.stringify({
          reporteId,
          userId,
          zona: item.zona,
          tipoProblema: item.tipoProblema,
          descripcion,
          imagen_mime_type: imagen_mime_type || null,
          imagen_base64: imagen_base64 || null,
        }),
      })
    );

    return {
      statusCode: 201,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({
        message: "Incidencia registrada y enviada a evaluación inteligente",
        reporte: item,
      }),
    };
  } catch (error) {
    console.error("Error registrando incidencia:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error interno", error: error.message }),
    };
  }
};
