"""
Trabajo pesado de un modelo. Corre SIEMPRE en un proceso hijo aparte (ver app.py):
al terminar, el proceso muere y el sistema recupera toda su memoria, aunque
OpenCascade o Python hayan dejado memoria fragmentada.
"""
import logging
import os
import tempfile

import httpx

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("geometria")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
for sufijo in ("/rest/v1", "/storage/v1"):
    if SUPABASE_URL.endswith(sufijo):
        SUPABASE_URL = SUPABASE_URL[: -len(sufijo)]
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
BUCKET = "modelos-3d"
MAX_MB = int(os.environ.get("MAX_MB", "80"))


def _h(extra=None):
    h = {"apikey": SERVICE_KEY, "Authorization": f"Bearer {SERVICE_KEY}"}
    if extra:
        h.update(extra)
    return h


def actualizar(cli, modelo_id, datos):
    r = cli.patch(f"{SUPABASE_URL}/rest/v1/modelos_3d", params={"id": f"eq.{modelo_id}"},
                  headers=_h({"Content-Type": "application/json", "Prefer": "return=minimal"}),
                  json=datos)
    r.raise_for_status()


def _descargar(cli, path, sufijo):
    """Descarga el archivo directo a disco (no lo carga completo en memoria)."""
    limite = MAX_MB * 1024 * 1024
    f = tempfile.NamedTemporaryFile(suffix=sufijo, delete=False)
    try:
        with cli.stream("GET", f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{path}", headers=_h()) as r:
            r.raise_for_status()
            leidos = 0
            for trozo in r.iter_bytes(1024 * 1024):
                leidos += len(trozo)
                if leidos > limite:
                    raise ValueError(f"Archivo mayor a {MAX_MB} MB")
                f.write(trozo)
        f.close()
        return f.name
    except Exception:
        f.close()
        os.unlink(f.name)
        raise


def procesar_modelo(modelo_id: str):
    """Descarga, procesa y sube un modelo. Deja el resultado o el error en modelos_3d."""
    from geometria import procesar  # OpenCascade solo se carga en el proceso hijo

    with httpx.Client(timeout=120) as cli:
        try:
            r = cli.get(f"{SUPABASE_URL}/rest/v1/modelos_3d",
                        params={"id": f"eq.{modelo_id}", "select": "id,formato,archivo_path"},
                        headers=_h())
            r.raise_for_status()
            filas = r.json()
            if not filas:
                log.error("Modelo %s: no se encontró el registro en modelos_3d", modelo_id)
                return
            fila = filas[0]
            actualizar(cli, modelo_id, {"estado": "procesando", "error_msg": None})

            sufijo = ".stp" if fila["formato"] == "step" else ".igs"
            ruta = _descargar(cli, fila["archivo_path"], sufijo)
            try:
                glb, resultado = procesar(ruta, fila["formato"])
            finally:
                os.unlink(ruta)

            glb_path = f"{modelo_id}/modelo.glb"
            r = cli.post(f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{glb_path}",
                         headers=_h({"Content-Type": "model/gltf-binary", "x-upsert": "true"}),
                         content=glb)
            r.raise_for_status()
            del glb
            actualizar(cli, modelo_id, {"estado": "listo", "glb_path": glb_path, "caras": resultado})
            log.info("Modelo %s listo: %s caras, %s triángulos",
                     modelo_id, resultado["num_caras"], resultado["num_triangulos"])
        except Exception as e:  # el error queda en los logs de Render y visible en la app
            detalle = str(e)
            if isinstance(e, httpx.HTTPStatusError):
                detalle = f"Supabase respondió {e.response.status_code}: {e.response.text[:300]}"
            log.error("Modelo %s falló: %s", modelo_id, detalle)
            try:
                actualizar(cli, modelo_id, {"estado": "error", "error_msg": detalle[:500]})
            except Exception as e2:
                log.error("Tampoco se pudo marcar el error en Supabase: %s", e2)


def probar_archivo(ruta: str, formato: str):
    """Para /probar: regresa solo el JSON de caras."""
    from geometria import procesar
    _, resultado = procesar(ruta, formato)
    return resultado
