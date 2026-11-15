exports.handler = async (event) => {
  const { zona, descripciones, antenasNormalizadas } = event;

  const payload = {
    zona,
    reportes: descripciones,
    antenas_estado: antenasNormalizadas,
  };

  const response = await fetch(
    `${process.env.GCP_AI_BASE_URL}/api/v1/ai/diagnose-incident`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );

  if (!response.ok) {
    throw new Error(`Error en GCP Diagnose Incident IA: HTTP ${response.status}`);
  }

  const diagnosticoIA = await response.json();

  return {
    ...event,
    diagnosticoIA,
  };
};