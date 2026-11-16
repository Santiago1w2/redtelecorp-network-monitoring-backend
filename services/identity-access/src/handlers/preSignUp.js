exports.handler = async (event) => {
  // Cognito debe confirmar el correo; conocer un email de ADMIN_EMAILS no concede acceso.
  event.response.autoConfirmUser = false;
  event.response.autoVerifyEmail = false;
  return event;
};
