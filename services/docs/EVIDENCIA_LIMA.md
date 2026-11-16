# Dataset ficticio de evidencia: Lima y Callao

Generador: `services/scripts/seed_lima_evidence.py`. Utiliza exclusivamente las APIs públicas del despliegue dev y Cognito; no escribe directamente en tablas ni reemplaza registros reales.

## Contenido autorizado y cargado

- 100 cuentas: 1 admin, 4 supervisores de red, 5 operadores de soporte, 30 técnicos y 60 clientes.
- 100 zonas poligonales ficticias: sectores médicos, educativos, residenciales, industriales y comerciales.
- 100 estaciones ficticias alrededor de 20 distritos/sectores de Lima Metropolitana y Callao.
- Las 20 antenas `ANT-SIM-...` proporcionadas por el usuario, con coordenadas y coberturas de 1400–2150 metros.
- 30 perfiles de técnicos vinculados por ID a sus cuentas Cognito, especialidades NETWORK/4G/5G.
- 100 mediciones: cinco muestras por antena en cinco días, con tráfico normal, caída, saturación e inhabilitación.
- 100 reportes creados como clientes ficticios mediante `POST /incidencias`.

Los primeros 60 reportes forman tres cohortes de 20 clientes distintos, cada una con la misma zona/tipo y menos de 24 horas entre reportes. Los restantes son casos aislados de lentitud, llamadas, mensajes y falta de cobertura. Las zonas y descripciones contienen marcas EVIDENCIA/SIMULADA/FICTICIA; los servicios usan identificadores SIM-SERV. Las posiciones no representan antenas ni edificios de una empresa real.

Los incidentes colectivos, diagnósticos IA, recurrencia, órdenes y notificaciones se generan mediante el flujo normal del backend. No se insertan cien incidentes u órdenes inventadas sin relaciones para alcanzar una cifra arbitraria.

El usuario autorizó expresamente las notificaciones al administrador durante esta carga. Las cuentas ficticias usan `example.invalid` y no tienen teléfonos. El correo real del administrador puede recibir múltiples notificaciones del lote; las direcciones de prueba no son buzones que deban confirmarse para correo SNS.

## Ejecución y reanudación

Python 3, biblioteca estándar. La clave existente del administrador se solicita de forma oculta o se toma de `RTC_SEED_ADMIN_PASSWORD`; no se incluye en el código ni se imprime. El archivo de IDs debe tener la forma `{"antenna_ids": ["ANT-SIM-..."]}` con los veinte valores autorizados.

```sh
python services/scripts/seed_lima_evidence.py \
  --directory /ruta/privada/fuera-de-git/evidencia-lima \
  --antenna-ids /ruta/antenas-simuladas-20.json
```

Sin `--apply` solo genera el plan. Para efectuar la carga previamente autorizada:

```sh
python services/scripts/seed_lima_evidence.py \
  --directory /ruta/privada/fuera-de-git/evidencia-lima \
  --antenna-ids /ruta/antenas-simuladas-20.json \
  --apply --allow-admin-email
```

Conservar la misma carpeta y el mismo `dataset.json`/`progress.json` para reanudar. `--phase` acepta `users`, `base`, `telemetry`, `reports` o `all`. El orden es usuarios → base → telemetría → reportes. No ejecutar dos procesos de carga contra la misma carpeta simultáneamente.

Se guardan checkpoints después de cada recurso. Las zonas/estaciones/antenas existentes se comprueban ante HTTP 409; si difieren se detiene sin sobrescribirlas. Las coberturas PUT de las veinte antenas autorizadas sí se establecen con el plan. La telemetría usa los IDs de eventos derivados por el backend. Los reportes utilizan Idempotency-Key y fechas fijas del manifest para evitar duplicados al reanudar.

El API administrativo crea contraseñas temporales con mensajes de invitación suprimidos. El generador completa el desafío de primera contraseña únicamente para las cuentas ficticias recién creadas. Después verifica perfil y rol. Las claves finales aleatorias se guardan en `CREDENCIALES_PRIVADAS.json` y `CUENTAS_DE_PRUEBA_PRIVADAS.csv` dentro de la carpeta privada, nunca en Git ni en el reporte público. No modifica la contraseña del administrador existente.

## Evidencias de esta ejecución

Los archivos están en la carpeta privada de salida `outputs/evidencia-lima` de la sesión de trabajo:

- `dataset.json`: solicitudes planificadas, sin contraseñas.
- `progress.json`: checkpoints y respuestas de creación, sin tokens.
- `verification.json`: lecturas de API posteriores a la carga, incluidos historial, colectivos, órdenes y notificaciones.
- `summary.json`: conteos verificados del lote.
- `additional-evidence.json`: cambios de estado mediante API y pruebas de permisos con las cinco cuentas representativas.
- `oracle-impact.json`: respuesta de análisis geográfico real con Oracle.
- `REPORTE.md`: resultados y ejemplos para la demostración.
- `CUENTAS_DE_PRUEBA_PRIVADAS.csv`: credenciales de las cien cuentas; no publicar.

La verificación distingue HTTP 202 (aceptado para proceso) de persistencia: vuelve a consultar los cien reportes y compara las cien mediciones por ID de antena, fecha y tráfico contra el historial. Los conteos de incidentes y estados pueden crecer/cambiar si el simulador programado sigue activo. No se borra evidencia previa ni se desactiva el simulador.

Los diagnósticos con `GCP_VERTEX_AI` son respuestas del proveedor de IA sobre datos ficticios, no comprobaciones de causas físicas. `GCP_VERTEX_AI_RECURRENCE_RULE` indica que también se aplicó la regla de recurrencia del backend. La respuesta Oracle `VERIFIED_REQUEST` acredita que se ejecutó esa integración en la solicitud de impacto del lote.
