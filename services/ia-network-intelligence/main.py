import os
import json
import base64
from fastapi import FastAPI, HTTPException, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import List, Optional
from google import genai
from google.genai import types

app = FastAPI(
    title="Red TeleCorp - MS6 AI Network Intelligence",
    description="Microservicio de Inteligencia Artificial en Google Cloud Run + Vertex AI para triaje, diagnóstico de red y soporte.",
    version="1.0.0"
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

API_SECRET_TOKEN = os.environ.get("API_SECRET_TOKEN")
GCP_PROJECT = os.environ.get("GCP_PROJECT", "red-telecorp")
GCP_LOCATION = os.environ.get("GCP_LOCATION", "global")
MODEL_ID = os.environ.get("MODEL_ID", "gemini-3.5-flash-lite")

client = genai.Client(vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION)


class ReporteUsuario(BaseModel):
    usuario_id: str
    zona: str
    descripcion: str
    imagen_base64: Optional[str] = None
    imagen_mime_type: Optional[str] = "image/jpeg"


class IncidenteColectivoRequest(BaseModel):
    zona: str
    reportes: List[str]
    antenas_estado: List[dict]


class ChatRequest(BaseModel):
    mensaje: str
    historial: Optional[List[str]] = []


def verificar_token(x_api_key: str = Header(None)):
    if not API_SECRET_TOKEN or x_api_key != API_SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="No autorizado")


@app.get("/")
def health_check():
    return {
        "status": "online",
        "servicio": "MS6 AI Network Intelligence",
        "modelo": MODEL_ID,
        "multimodal": True
    }


@app.post("/api/v1/ai/prioritize")
def priorizar_reporte(data: ReporteUsuario, x_api_key: str = Header(None)):
    verificar_token(x_api_key)
    try:
        prompt = f"""
        Analiza este reporte de telecomunicaciones de Red TeleCorp en la zona '{data.zona}':
        "{data.descripcion}"
        
        Si se adjunta una imagen, evalúa el estado físico del equipo, cables, postes o luces del router
        para detectar daños físicos, peligros eléctricos o fallas de hardware.
        
        Devuelve SOLO un JSON válido con esta estructura exacta:
        {{
          "categoria": "Sin internet | Falla de voz | Intermitencia | Daño físico",
          "prioridad": "BAJA | MEDIA | ALTA | CRITICA",
          "es_infraestructura_critica": true,
          "evidencia_visual_detectada": "descripción breve de lo que se ve en la imagen o null si no hay imagen",
          "resumen_tecnico": "resumen en 1 línea"
        }}
        """
        contenidos = [prompt]

        if data.imagen_base64:
            b64_limpio = data.imagen_base64.split(",")[-1]
            imagen_bytes = base64.b64decode(b64_limpio)
            contenidos.append(
                types.Part.from_bytes(
                    data=imagen_bytes,
                    mime_type=data.imagen_mime_type or "image/jpeg"
                )
            )

        response = client.models.generate_content(
            model=MODEL_ID,
            contents=contenidos,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return json.loads(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/ai/diagnose-incident")
def diagnosticar_incidente(data: IncidenteColectivoRequest, x_api_key: str = Header(None)):
    verificar_token(x_api_key)
    try:
        prompt = f"""
        Eres el sistema de IA de Red TeleCorp. En la zona '{data.zona}' se detectaron {len(data.reportes)} reportes en menos de 24h.
        Reglas de red: < 60 GB/min = CAIDA, 0 GB/min = INHABILITADA, > 129 GB/min = SATURADA.
        
        Estado de antenas en la zona: {json.dumps(data.antenas_estado)}
        Muestra de reportes de usuarios: {json.dumps(data.reportes[:15])}
        
        Devuelve SOLO un JSON válido con:
        {{
          "causa_raiz_probable": "explicación técnica correlacionando antenas y reportes",
          "riesgo_estructural": "BAJO | MEDIO | ALTO",
          "requiere_cuadrilla_campo": true,
          "mensaje_notificacion_usuario": "mensaje claro y empático para enviar por SMS/Email a los afectados"
        }}
        """
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json")
        )
        return json.loads(response.text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/v1/ai/chat")
def asistente_virtual(data: ChatRequest):
    try:
        instruccion = """
        Eres el asistente virtual de soporte técnico de Red TeleCorp.
        Ayuda al usuario a diagnosticar brevemente su problema de señal/internet y pídele los datos clave 
        para registrar su incidencia: identificación, número de teléfono, ubicación/zona y descripción del problema.
        Sé amable, directo y conciso.
        """
        response = client.models.generate_content(
            model=MODEL_ID,
            contents=data.mensaje,
            config=types.GenerateContentConfig(system_instruction=instruccion)
        )
        return {"respuesta": response.text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
