"""
Servicio de geometría para Modelos 3D de dinmec-app.

POST /procesar  {"modelo_id": "<uuid>"}   (header X-Clave: <CLAVE_SERVICIO>)
  1. Lee el registro en modelos_3d
  2. Descarga el STEP/IGES del bucket 'modelos-3d'
  3. Extrae la geometría exacta de cada cara y genera el .glb
  4. Sube el .glb al bucket y guarda caras + estado 'listo' en la tabla

POST /probar (multipart, campo "archivo") -> JSON con las caras (para pruebas, no toca Supabase)
GET  /salud
"""
import os
import tempfile

import httpx
from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from geometria import procesar

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
CLAVE = os.environ.get("CLAVE_SERVICIO", "")
BUCKET = "modelos-3d"
ORIGENES = [o.strip() for o in os.environ.get("ORIGENES", "*").split(",") if o.strip()]
MAX_MB = int(os.environ.get("MAX_MB", "80"))

app = FastAPI(title="Geometría Dinmec")
app.add_middleware(CORSMiddleware, allow_origins=ORIGENES, allow_methods=["*"], allow_headers=["*"])


def _h(extra=None):
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    if extra:
        h.update(extra)
    return h


def _actualizar(cli, modelo_id, datos):
    r = cli.patch(f"{SUPABASE_URL}/rest/v1/modelos_3d", params={"id": f"eq.{modelo_id}"},
                  headers=_h({"Content-Type": "application/json", "Prefer": "return=minimal"}),
                  json=datos)
    r.raise_for_status()


def _trabajo(modelo_id: str):
    with httpx.Client(timeout=120) as cli:
        try:
            r = cli.get(f"{SUPABASE_URL}/rest/v1/modelos_3d",
                        params={"id": f"eq.{modelo_id}", "select": "id,formato,archivo_path"},
                        headers=_h())
            r.raise_for_status()
            filas = r.json()
            if not filas:
                return
            fila = filas[0]
            _actualizar(cli, modelo_id, {"estado": "procesando", "error_msg": None})

            r = cli.get(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{fila['archivo_path']}", headers=_h())
            r.raise_for_status()
            if len(r.content) > MAX_MB * 1024 * 1024:
                raise ValueError(f"Archivo mayor a {MAX_MB} MB")

            sufijo = ".stp" if fila["formato"] == "step" else ".igs"
            with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as f:
                f.write(r.content)
                ruta = f.name
            try:
                glb, resultado = procesar(ruta, fila["formato"])
            finally:
                os.unlink(ruta)

            glb_path = f"{modelo_id}/modelo.glb"
            r = cli.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{glb_path}",
                         headers=_h({"Content-Type": "model/gltf-binary", "x-upsert": "true"}),
                         content=glb)
            r.raise_for_status()
            _actualizar(cli, modelo_id, {"estado": "listo", "glb_path": glb_path, "caras": resultado})
        except Exception as e:  # el error queda visible en la app
            try:
                _actualizar(cli, modelo_id, {"estado": "error", "error_msg": str(e)[:500]})
            except Exception:
                pass


class Solicitud(BaseModel):
    modelo_id: str


@app.get("/salud")
def salud():
    return {"ok": True, "supabase": bool(SUPABASE_URL and SERVICE_KEY)}


@app.post("/procesar")
def procesar_modelo(sol: Solicitud, tareas: BackgroundTasks, x_clave: str = Header(default="")):
    if not CLAVE or x_clave != CLAVE:
        raise HTTPException(401, "Clave inválida")
    if not (SUPABASE_URL and SERVICE_KEY):
        raise HTTPException(500, "Servicio sin configurar")
    tareas.add_task(_trabajo, sol.modelo_id)
    return {"ok": True, "modelo_id": sol.modelo_id, "estado": "procesando"}


@app.post("/probar")
async def probar(archivo: UploadFile = File(...), x_clave: str = Header(default="")):
    if not CLAVE or x_clave != CLAVE:
        raise HTTPException(401, "Clave inválida")
    nombre = (archivo.filename or "").lower()
    if nombre.endswith((".stp", ".step")):
        formato, sufijo = "step", ".stp"
    elif nombre.endswith((".igs", ".iges")):
        formato, sufijo = "iges", ".igs"
    else:
        raise HTTPException(400, "Solo STEP o IGES")
    with tempfile.NamedTemporaryFile(suffix=sufijo, delete=False) as f:
        f.write(await archivo.read())
        ruta = f.name
    try:
        _, resultado = procesar(ruta, formato)
    except ValueError as e:
        raise HTTPException(422, str(e))
    finally:
        os.unlink(ruta)
    return resultado
