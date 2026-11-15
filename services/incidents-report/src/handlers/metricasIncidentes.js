const { DynamoDBClient } = require("@aws-sdk/client-dynamodb");
const { DynamoDBDocumentClient, ScanCommand } = require("@aws-sdk/lib-dynamodb");
const { isStaff, response } = require("./auth");

const ddb = DynamoDBDocumentClient.from(new DynamoDBClient({}));

exports.handler = async (event) => {
  try {
    if (!isStaff(event)) return response(403, { message: "Se requiere un rol interno" });
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
        if (ant.estado !== "OPERATIVA") {
          antenasFallas[ant.antena_id] = (antenasFallas[ant.antena_id] || 0) + 1;
        }
      }
    }

    return {
      statusCode: 200,
      headers: { "Access-Control-Allow-Origin": "*" },
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
    console.error("Error calculando métricas:", error);
    return {
      statusCode: 500,
      headers: { "Access-Control-Allow-Origin": "*" },
      body: JSON.stringify({ message: "Error interno", error: error.message }),
    };
  }
};
