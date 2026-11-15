# TeleCorp Identity & Access Microservice (`telecorp-identity-service`)

Microservicio de autenticación, autorización y gestión del ciclo de vida de usuarios para **Red TeleCorp**, construido sobre arquitectura Serverless en AWS (Amazon Cognito, AWS Lambda, API Gateway, DynamoDB y Systems Manager Parameter Store).

---

## 1. Arquitectura y Stack Tecnológico

* **Framework:** Serverless Framework v4 (`nodejs22.x`)
* **Identidad y Control de Acceso:** Amazon Cognito User Pools + User Pool Groups
* **Base de Datos de Perfiles:** Amazon DynamoDB (`telecorp-identity-service-users-{stage}`)
* **API & Autorización:** Amazon API Gateway (REST API) protegido con `COGNITO_USER_POOLS` Authorizer
* **Configuración Centralizada:** AWS Systems Manager (SSM Parameter Store)
* **Rol de Ejecución:** AWS Academy `LabRole`

### Estructura del Proyecto
```text
├── serverless.yml                  # Infraestructura como código (IaC) y triggers
├── README.md                       # Documentación técnica y flujos de trabajo
└── src/
    └── handlers/
        ├── preSignUp.js            # Trigger: Autoconfirmación de administradores iniciales
        ├── postConfirmation.js     # Trigger: Asignación de grupo base y registro en DynamoDB
        ├── getProfile.js           # GET /auth/perfil - Consulta de perfil autenticado
        ├── crearUsuario.js         # POST /auth/usuarios - Creación de personal interno (Admin)
        └── asignarRol.js           # PUT /auth/usuarios/rol - Reasignación de roles (Admin)
```

---

## 2. Matriz de Roles (Cognito Groups)

El sistema maneja 5 grupos jerárquicos en Amazon Cognito, los cuales se propagan automáticamente dentro del claim `cognito:groups` del `IdToken`:

| Rol (`GroupName`) | Precedencia | Descripción | Método de Alta |
| :--- | :---: | :--- | :--- |
| **`admin`** | 1 | Control total del sistema, creación de cuentas internas y gestión de roles. | `SignUp` (Whitelisted en SSM) o creado por otro Admin |
| **`supervisor_red`** | 2 | Monitoreo de antenas, nodos e infraestructura de red. | Creado por `admin` (`POST /auth/usuarios`) |
| **`operador_soporte`** | 3 | Mesa de ayuda, recepción y triaje de incidencias. | Creado por `admin` (`POST /auth/usuarios`) |
| **`tecnico_campo`** | 4 | Personal en campo encargado de reparación física de equipos. | Creado por `admin` (`POST /auth/usuarios`) |
| **`clientes`** | 5 | Usuarios finales que reportan fallas y consultan estado del servicio. | Registro público (`SignUp` + `ConfirmSignUp`) |

---

## 3. Configuración y Despliegue

### Prerrequisitos en AWS SSM Parameter Store
Antes del primer despliegue, debe existir el parámetro con los correos autorizados como administradores iniciales (separados por coma si hay más de uno):

```bash
aws ssm put-parameter \
  --name "/telecorp/dev/cognito/admin-emails" \
  --value "admin@telecorp.com" \
  --type "String" \
  --overwrite
```

### Despliegue
```bash
npm install
serverless deploy
```

### Parámetros Exportados Automáticamente (SSM)
Al finalizar el despliegue, el stack publica los siguientes valores estáticos para que otros microservicios y el Frontend los consuman sin hardcodear IDs:

* `/telecorp/{stage}/cognito/user-pool-id` — ID del User Pool.
* `/telecorp/{stage}/cognito/user-pool-arn` — ARN del User Pool (para los Authorizers de otros microservicios).
* `/telecorp/{stage}/cognito/client-id` — `ClientId` público para el Frontend y Postman (permanece constante entre despliegues).

> **Nota sobre Dependencias Circulares:** En `serverless.yml`, los triggers `PreSignUp` y `PostConfirmation` están enlazados directamente mediante `LambdaConfig` y `AWS::Lambda::Permission`. La variable `USER_POOL_ID` se declara exclusivamente a nivel de función en `crearUsuario` y `asignarRol` para evitar ciclos de referencia en CloudFormation.

---

## 4. Flujos de Trabajo End-to-End

### Flujo A: Alta e Inicio de Sesión del Administrador Inicial (Bootstrap)
1. **Registro (`SignUp` en Cognito):** El administrador se registra con el correo listado en `/telecorp/dev/cognito/admin-emails`.
2. **Automatización Interna:**
   * `preSignUp` detecta el correo autorizado y marca `autoConfirmUser = true` y `autoVerifyEmail = true`.
   * `postConfirmation` lo añade al grupo `admin` y crea su documento en DynamoDB.
3. **Autenticación (`InitiateAuth`):** Inicia sesión directamente con `USER_PASSWORD_AUTH` y obtiene su `IdToken` con privilegios de administrador.

---

### Flujo B: Creación de Personal Interno y Contraseña Temporal Obligatoria
Para roles internos (`supervisor_red`, `operador_soporte`, `tecnico_campo`), el administrador provisiona las cuentas con una contraseña temporal (`Permanent: false`), obligando al trabajador a definir su contraseña definitiva en su primer acceso.

1. **Provisionamiento (`POST /auth/usuarios`):**
   * El `admin` envía los datos del trabajador junto con una contraseña temporal.
   * Cognito crea el usuario en estado `FORCE_CHANGE_PASSWORD`, lo asigna al grupo indicado y guarda su perfil en DynamoDB.
2. **Primer Inicio de Sesión (`InitiateAuth`):**
   * El trabajador intenta iniciar sesión con su correo y la contraseña temporal.
   * Cognito intercepta el acceso y devuelve `"ChallengeName": "NEW_PASSWORD_REQUIRED"` junto con un token temporal `Session`.
3. **Definición de Contraseña Definitiva (`RespondToAuthChallenge`):**
   * El trabajador envía el `Session` recibido y su nueva contraseña definitiva (`NEW_PASSWORD`).
   * Cognito cambia el estado de la cuenta a `CONFIRMED`, descarta la contraseña temporal y entrega el `AuthenticationResult` (`IdToken`, `AccessToken`, `RefreshToken`).
4. **Accesos Posteriores:**
   * En adelante, el trabajador inicia sesión de forma estándar mediante `InitiateAuth` con su nueva contraseña.

---

### Flujo C: Registro Público de Clientes Externos
1. **Registro (`SignUp` en Cognito):** El cliente se registra con `email`, `password`, `phone_number` y `custom:dni`.
2. **Verificación (`ConfirmSignUp`):** Ingresa el código de verificación enviado a su correo.
3. **Automatización (`postConfirmation`):** Se le asigna automáticamente el grupo `clientes` en Cognito y se registra su perfil en DynamoDB.

---

### Flujo D: Reasignación de Roles (`PUT /auth/usuarios/rol`)
1. El `admin` envía el `userId` (`sub`), `email` y `nuevoRol`.
2. La Lambda `asignarRol` remueve al usuario de sus grupos actuales en Cognito, lo añade al nuevo grupo y actualiza el atributo `role` en DynamoDB.
3. *Importante:* El usuario reasignado deberá refrescar su sesión (`REFRESH_TOKEN_AUTH` o nuevo login) para que el nuevo rol se refleje en su `IdToken`.

---

## 5. Referencia de Endpoints y Peticiones (API & Cognito)

### Endpoints REST (API Gateway)
**URL Base:** `https://{api-id}.execute-api.us-east-1.amazonaws.com/dev`  
**Autenticación:** Todos los endpoints requieren el header `Authorization: Bearer <IdToken>`. *(Debe usarse el `IdToken`, ya que contiene los grupos y atributos personalizados).*

#### 1. Obtener Perfil del Usuario Autenticado
* **Endpoint:** `GET /auth/perfil`
* **Acceso:** Cualquier usuario autenticado.
* **Respuesta (`200 OK`):**
  ```json
  {
    "user": {
      "userId": "94f8a458-5061-7066-f563-5a8cc3f8581f",
      "email": "supervisor1@telecorp.com",
      "dni": "45678912",
      "phone": "+51999888777",
      "role": "supervisor_red",
      "createdAt": "2026-11-14T21:00:00.000Z"
    },
    "cognitoGroups": ["supervisor_red"]
  }
  ```

#### 2. Crear Usuario Interno (Solo Admin)
* **Endpoint:** `POST /auth/usuarios`
* **Acceso:** Exclusivo rol `admin`.
* **Body:**
  ```json
  {
    "email": "supervisor1@telecorp.com",
    "password": "TempPassword123!",
    "dni": "45678912",
    "phone": "+51999888777",
    "role": "supervisor_red"
  }
  ```
* **Respuesta (`201 Created`):** Devuelve el objeto `user` creado con su `userId` (`sub`).

#### 3. Cambiar Rol de Usuario (Solo Admin)
* **Endpoint:** `PUT /auth/usuarios/rol`
* **Acceso:** Exclusivo rol `admin`.
* **Body:**
  ```json
  {
    "userId": "94f8a458-5061-7066-f563-5a8cc3f8581f",
    "email": "tecnico1@telecorp.com",
    "nuevoRol": "supervisor_red"
  }
  ```

---

### Peticiones Directas a Amazon Cognito (Frontend / Postman)
**Endpoint:** `POST https://cognito-idp.us-east-1.amazonaws.com/`  
**Header Obligatorio:** `Content-Type: application/x-amz-json-1.1`

#### A. Iniciar Sesión (`InitiateAuth`)
* **Header:** `X-Amz-Target: AWSCognitoIdentityProviderService.InitiateAuth`
* **Body:**
  ```json
  {
    "AuthFlow": "USER_PASSWORD_AUTH",
    "ClientId": "<TU_CLIENT_ID>",
    "AuthParameters": {
      "USERNAME": "supervisor1@telecorp.com",
      "PASSWORD": "TempPassword123!"
    }
  }
  ```

#### B. Responder Reto de Nueva Contraseña (`RespondToAuthChallenge`)
* **Header:** `X-Amz-Target: AWSCognitoIdentityProviderService.RespondToAuthChallenge`
* **Body:**
  ```json
  {
    "ChallengeName": "NEW_PASSWORD_REQUIRED",
    "ClientId": "<TU_CLIENT_ID>",
    "Session": "<SESSION_DEVUELTO_EN_INITIATE_AUTH>",
    "ChallengeResponses": {
      "USERNAME": "supervisor1@telecorp.com",
      "NEW_PASSWORD": "SuperPassword2026!"
    }
  }
  ```

---

## 6. Guía de Integración para Otros Microservicios

Para proteger endpoints en otros microservicios de **Red TeleCorp** utilizando este mismo proveedor de identidad:

1. **Referenciar el ARN desde SSM en el `serverless.yml` del otro microservicio:**
   ```yaml
   authorizer:
     name: TeleCorpAuthorizer
     type: COGNITO_USER_POOLS
     arn: ${ssm:/telecorp/${self:provider.stage}/cognito/user-pool-arn}
   ```
2. **Leer identidad y rol dentro de cualquier Lambda:**
   ```javascript
   const claims = event.requestContext.authorizer?.claims || {};
   const userId = claims.sub;
   const email = claims.email;
   const roles = claims["cognito:groups"] || []; // Ej: "supervisor_red" o ["supervisor_red"]
   ```