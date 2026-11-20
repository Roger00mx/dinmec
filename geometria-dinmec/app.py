"""
Servicio de geometría para Modelos 3D de dinmec-app.

POST /procesar  {"modelo_id": "<uuid>"}   (header X-Clave: <CLAVE_SERVICIO>)
  Mete el modelo a la fila. Un solo trabajador los procesa UNO POR UNO, cada uno
  en un proceso hijo nuevo (trabajo.py):
  1. Lee el registro en modelos_3d
  2. Descarga el STEP/IGES del bucket 'modelos-3d'
  3. Extrae la geometría exacta de cada cara y genera el .glb
  4. Sube el .glb al bucket y guarda caras + estado 'listo' en la tabla

POST /probar (multipart, campo "archivo") -> JSON con las caras (para pruebas, no toca Supabase)
GET  /salud

Por qué así (nov 2026): cuando se subían varios modelos seguidos se procesaban al mismo
tiempo y la memoria rebasaba los 512 MB de Render, que reiniciaba el servicio.
Ahora: uno a la vez, y el proceso hijo devuelve toda su memoria al terminar.
Este proceso principal nunca carga OpenCascade.
"""
import logging
import os
import queue
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor, TimeoutError as FuturoTimeout
from concurrent.futures.process import BrokenProcessPool
from multiprocessing import get_context

import httpx
from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

import trabajo
from trabajo import SUPABASE_URL, SERVICE_KEY, _h, actualizar

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("geometria")

CLAVE = os.environ.get("CLAVE_SERVICIO", "").strip()
ORIGENES = [o.strip() for o in os.environ.get("ORIGENES", "*").split(",") if o.strip()]
MAX_MB = trabajo.MAX_MB
LIMITE_MIN = int(os.environ.get("LIMITE_MIN", "15"))  # minutos máximos por modelo

app = FastAPI(title="Geometría Dinmec")
app.add_middleware(CORSMiddleware, allow_origins=ORIGENES, allow_methods=["*"], allow_headers=["*"])


# ---------- Ejecución de un trabajo en un proceso hijo (uno a la vez) ----------

_candado = threading.Lock()  # garantiza un solo modelo en memoria a la vez


def _en_hijo(funcion, *args):
    """Corre funcion(*args) en un proceso nuevo y espera el resultado.

    Si el hijo se queda sin memoria y el sistema lo mata, el servicio sigue vivo
    y se lanza MemoryError con un mensaje claro.
    """
    with _candado:
        ejecutor = ProcessPoolExecutor(max_workers=1, mp_context=get_context("spawn"))
        try:
            futuro = ejecutor.submit(funcion, *args)
            try:
                return futuro.result(timeout=LIMITE_MIN * 60)
            except BrokenProcessPool:
                raise MemoryError("El modelo es demasiado grande para la memoria del servidor")
            except FuturoTimeout:
                for p in list(getattr(ejecutor, "_processes", {}).values()):
                    p.kill()
                raise TimeoutError(f"El modelo tardó más de {LIMITE_MIN} min en procesarse")
        finally:
            ejecutor.shutdown(wait=True, cancel_futures=True)


# ---------- Fila de modelos ----------

_fila: "queue.Queue[str]" = queue.Queue()
_en_fila: set = set()
_fila_candado = threading.Lock()


def encolar(modelo_id: str) -> bool:
    with _fila_candado:
        if modelo_id in _en_fila:
            return False
        _en_fila.add(modelo_id)
    _fila.put(modelo_id)
    return True


def _marcar_error(modelo_id, mensaje):
    try:
        with httpx.Client(timeout=30) as cli:
            actualizar(cli, modelo_id, {"estado": "error", "error_msg": mensaje[:500]})
    except Exception as e:
        log.error("No se pudo marcar el error de %s en Supabase: %s", modelo_id, e)


def _trabajador():
    while True:
        modelo_id = _fila.get()
        try:
            log.info("Procesando %s (%s más en la fila)", modelo_id, _fila.qsize())
            _en_hijo(trabajo.procesar_modelo, modelo_id)
        except (MemoryError, TimeoutError) as e:
            log.error("Modelo %s falló: %s", modelo_id, e)
            _marcar_error(modelo_id, str(e))
        except Exception as e:
            log.exception("Modelo %s: error inesperado", modelo_id)
            _marcar_error(modelo_id, f"Error inesperado: {e}")
        finally:
            with _fila_candado:
                _en_fila.discard(modelo_id)
            _fila.task_done()


def _recuperar_pendientes():
    """Al arrancar (p. ej. tras un reinicio de Render) retoma los modelos que quedaron a medias."""
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/modelos_3d",
                      params={"select": "id", "estado": "in.(pendiente,procesando)", "order": "created_at"},
                      headers=_h(), timeout=20)
        r.raise_for_status()
        ids = [f["id"] for f in r.json()]
        for i in ids:
            encolar(i)
        if ids:
            log.info("Se retomaron %s modelos pendientes", len(ids))
    except Exception as e:
        log.error("No se pudieron revisar los modelos pendientes: %s", e)


class Solicitud(BaseModel):
    modelo_id: str


@app.get("/salud")
def salud():
    return {"ok": True, "supabase": bool(SUPABASE_URL and SERVICE_KEY), "en_fila": _fila.qsize()}


@app.on_event("startup")
def arrancar():
    """Arranca el trabajador, prueba la conexión y retoma pendientes."""
    threading.Thread(target=_trabajador, daemon=True, name="trabajador").start()
    if not (SUPABASE_URL and SERVICE_KEY):
        log.error("Faltan SUPABASE_URL o SUPABASE_SERVICE_KEY")
        return
    try:
        r = httpx.get(f"{SUPABASE_URL}/rest/v1/modelos_3d", params={"select": "id", "limit": "1"},
                      headers=_h(), timeout=20)
        if r.status_code == 200:
            log.info("Conexión a Supabase OK (%s)", SUPABASE_URL)
            _recuperar_pendientes()
        else:
            log.error("Supabase respondió %s al arrancar: %s", r.status_code, r.text[:300])
    except Exception as e:
        log.error("No se pudo conectar a Supabase (%s): %s", SUPABASE_URL, e)


@app.post("/procesar")
def procesar_modelo(sol: Solicitud, x_clave: str = Header(default="")):
    if not CLAVE or x_clave != CLAVE:
        raise HTTPException(401, "Clave inválida")
    if not (SUPABASE_URL and SERVICE_KEY):
        raise HTTPException(500, "Servicio sin configurar")
    encolar(sol.modelo_id)
    return {"ok": True, "modelo_id": sol.modelo_id, "estado": "procesando", "en_fila": _fila.qsize()}


@app.post("/probar")
def probar(archivo: UploadFile = File(...), x_clave: str = Header(default="")):
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
        while trozo := archivo.file.read(1024 * 1024):
            f.write(trozo)
        ruta = f.name
    try:
        return _en_hijo(trabajo.probar_archivo, ruta, formato)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except (MemoryError, TimeoutError) as e:
        raise HTTPException(507, str(e))
    finally:
        os.unlink(ruta)
