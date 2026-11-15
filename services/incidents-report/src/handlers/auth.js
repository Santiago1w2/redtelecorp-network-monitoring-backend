const STAFF_GROUPS = new Set(["admin", "supervisor_red", "operador_soporte", "tecnico_campo"]);

function groupsFrom(event) {
  const raw = event?.requestContext?.authorizer?.claims?.["cognito:groups"];
  if (Array.isArray(raw)) return raw;
  if (typeof raw !== "string" || !raw.trim()) return [];
  try {
    const parsed = JSON.parse(raw);
    if (Array.isArray(parsed)) return parsed;
  } catch (_) {}
  return raw.split(/[\s,]+/).filter(Boolean);
}

function claimsFrom(event) {
  return event?.requestContext?.authorizer?.claims || {};
}

function isStaff(event) {
  return groupsFrom(event).some((group) => STAFF_GROUPS.has(group));
}

function response(statusCode, payload) {
  return {
    statusCode,
    headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" },
    body: JSON.stringify(payload),
  };
}

module.exports = { claimsFrom, groupsFrom, isStaff, response };
