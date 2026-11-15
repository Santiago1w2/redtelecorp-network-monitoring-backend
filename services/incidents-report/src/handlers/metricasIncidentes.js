const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, ScanCommand } = require("@aws-sdk/lib-dynamodb");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    const claims = event.requestContext?.authorizer?.claims || {};
    let grupos = claims['cognito:groups'] || [];
    if (typeof grupos === 'string') { try { grupos = JSON.parse(grupos); } catch (_) { grupos = grupos.replace(/^\[|\]$/g, '').split(','); } }
    const staff = Array.isArray(grupos) && grupos.some(group => ['admin', 'supervisor_red', 'operador_soporte', 'tecnico_campo'].includes(group.trim()));
    if (!staff) return { statusCode: 403, body: '{}' };
    const [reportesRes, colectivosRes] = await Promise.all([
      ddb.send(new ScanCommand({ TableName: process.env.REPORTES_TABLE })),
      ddb.send(new ScanCommand({ TableName: process.env.INCIDENTES_TABLE })),
    ]);

    const reportes = reportesRes.Items || [];
    const colectivos = colectivosRes.Items || [];

    const porZona = {};
    const porCategoria = {};
    const antenasFallas = {};

    for (const r of reportes) {
      porZona[r.zona] = (porZona[r.zona] || 0) + 1;
      const cat = r.categoriaIA || r.tipoProblema || "Otros";
      porCategoria[cat] = (porCategoria[cat] || 0) + 1;
    }

    let sumaMinutosResolucion = 0;
    let totalResueltos = 0;

    for (const inc of colectivos) {
      if (typeof inc.tiempoResolucionMinutos === "number") {
        sumaMinutosResolucion += inc.tiempoResolucionMinutos;
        totalResueltos++;
      }
      for (const ant of inc.antenasAfectadas || []) {
        if (["DOWN", "DISABLED", "SATURATED"].includes(ant.status)) {
          antenasFallas[ant.antenna_id] = (antenasFallas[ant.antenna_id] || 0) + 1;
        }
      }
    }

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
      body: JSON.stringify({
        totalReportes: reportes.length,
        totalIncidentesColectivos: colectivos.length,
        tiempoPromedioResolucionMinutos:
          totalResueltos > 0 ? Math.round(sumaMinutosResolucion / totalResueltos) : null,
        zonasConMasIncidentes: porZona,
        tiposInconvenientesFrecuentes: porCategoria,
        antenasConMasFallas: antenasFallas,
      }),
    };
  } catch (error) {
    console.error("Error calculando mÃ©tricas:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": process.env.FRONTEND_ORIGIN },
      body: JSON.stringify({ message: "Error interno", error: error.message }),
    };
  }
};