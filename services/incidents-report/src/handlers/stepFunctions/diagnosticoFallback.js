exports.handler = async event => {
  const recurrence = event.recurrencia;
  const preventive = Boolean(recurrence);
  const cause = preventive
    ? `Falla recurrente: ${recurrence.failure_events} eventos en ${recurrence.affected_days} dias dentro de ${recurrence.window_days} dias. Se requiere inspeccion preventiva.`
    : 'Diagnostico automatico no disponible; requiere verificacion operativa.';
  return {
    ...event,
    diagnosticoIA: {
      causa_raiz_probable: cause,
      riesgo_estructural: preventive ? 'ALTO' : 'MEDIO',
      requiere_cuadrilla_campo: true,
      mensaje_notificacion_usuario: 'Nuestro equipo esta evaluando una incidencia en tu zona.',
      analysis_method: preventive ? 'RULES_AI_UNAVAILABLE_RECURRENCE' : 'RULES_AI_UNAVAILABLE',
    },
  };
};
