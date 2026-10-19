"""
Núcleo de geometría para Modelos 3D (dinmec-app).

Lee un STEP o IGES con OpenCascade y devuelve:
  - glb:   bytes de un modelo .glb ligero para el visor (three.js)
  - caras: lista con la geometría EXACTA de cada cara (plano, cilindro, cono, esfera...)
           y el rango de triángulos que le corresponde en el .glb, para que el visor
           sepa a qué cara pertenece el triángulo donde el usuario dio clic.

Unidades de salida: milímetros y grados.
"""
import json
import math
import struct

from OCP.STEPControl import STEPControl_Reader
from OCP.IGESControl import IGESControl_Reader
from OCP.IFSelect import IFSelect_RetDone
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.BRep import BRep_Tool
from OCP.BRepAdaptor import BRepAdaptor_Surface
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.GeomAbs import (
    GeomAbs_Plane, GeomAbs_Cylinder, GeomAbs_Cone, GeomAbs_Sphere, GeomAbs_Torus,
)
from OCP.TopAbs import TopAbs_FACE, TopAbs_REVERSED
from OCP.TopExp import TopExp_Explorer
from OCP.TopLoc import TopLoc_Location
from OCP.TopoDS import TopoDS


def leer_modelo(ruta: str, formato: str):
    """Lee STEP o IGES y regresa la forma (TopoDS_Shape) en milímetros."""
    formato = formato.lower()
    if formato in ("step", "stp"):
        lector = STEPControl_Reader()
    elif formato in ("iges", "igs"):
        lector = IGESControl_Reader()
    else:
        raise ValueError(f"Formato no soportado: {formato}")

    if lector.ReadFile(ruta) != IFSelect_RetDone:
        raise ValueError("No se pudo leer el archivo (¿está dañado o no es STEP/IGES?)")
    lector.TransferRoots()
    forma = lector.OneShape()
    if forma.IsNull():
        raise ValueError("El archivo no contiene geometría sólida")
    return forma


def _r(v, n=4):
    return round(float(v), n)


def _vec(p):
    return [_r(p.X()), _r(p.Y()), _r(p.Z())]


def _dir(d, invertir=False):
    s = -1.0 if invertir else 1.0
    return [_r(s * d.X(), 6), _r(s * d.Y(), 6), _r(s * d.Z(), 6)]


def geometria_cara(cara, idx):
    """Geometría analítica exacta de una cara."""
    sup = BRepAdaptor_Surface(cara, True)
    tipo = sup.GetType()
    invertida = cara.Orientation() == TopAbs_REVERSED

    props = GProp_GProps()
    BRepGProp.SurfaceProperties_s(cara, props)
    info = {"id": idx, "area": _r(props.Mass(), 3)}

    if tipo == GeomAbs_Plane:
        pl = sup.Plane()
        ax = pl.Axis()
        info.update(tipo="plano", punto=_vec(ax.Location()),
                    normal=_dir(ax.Direction(), invertida))
    elif tipo == GeomAbs_Cylinder:
        cil = sup.Cylinder()
        ax = cil.Axis()
        r = cil.Radius()
        info.update(tipo="cilindro", radio=_r(r), diametro=_r(2 * r),
                    eje_punto=_vec(ax.Location()), eje_dir=_dir(ax.Direction()),
                    # interior (barreno) si la normal apunta hacia el eje
                    barreno=bool(invertida))
    elif tipo == GeomAbs_Cone:
        co = sup.Cone()
        ax = co.Axis()
        info.update(tipo="cono", angulo_medio=_r(math.degrees(co.SemiAngle()), 4),
                    radio_ref=_r(co.RefRadius()),
                    eje_punto=_vec(ax.Location()), eje_dir=_dir(ax.Direction()))
    elif tipo == GeomAbs_Sphere:
        es = sup.Sphere()
        info.update(tipo="esfera", radio=_r(es.Radius()), diametro=_r(2 * es.Radius()),
                    centro=_vec(es.Location()))
    elif tipo == GeomAbs_Torus:
        to = sup.Torus()
        ax = to.Axis()
        info.update(tipo="toro", radio_mayor=_r(to.MajorRadius()),
                    radio_menor=_r(to.MinorRadius()),
                    eje_punto=_vec(ax.Location()), eje_dir=_dir(ax.Direction()))
    else:
        info.update(tipo="libre")  # B-spline u otra superficie libre
    return info


def triangular(forma, tolerancia_mm=None):
    """Malla para mostrar. La medición NO usa esta malla, usa la geometría exacta."""
    caja = Bnd_Box()
    BRepBndLib.Add_s(forma, caja)
    pmin, pmax = caja.CornerMin(), caja.CornerMax()
    xmin, ymin, zmin = pmin.X(), pmin.Y(), pmin.Z()
    xmax, ymax, zmax = pmax.X(), pmax.Y(), pmax.Z()
    diag = math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))
    if tolerancia_mm is None:
        tolerancia_mm = max(diag / 2000.0, 0.005)  # fino pero ligero
    BRepMesh_IncrementalMesh(forma, tolerancia_mm, False, 0.25, True)
    return {"min": [_r(xmin), _r(ymin), _r(zmin)], "max": [_r(xmax), _r(ymax), _r(zmax)]}


def procesar(ruta: str, formato: str):
    """Regresa (glb_bytes, resultado_dict)."""
    forma = leer_modelo(ruta, formato)
    caja = triangular(forma)

    posiciones, normales, indices = [], [], []
    caras = []
    tri_total = 0

    exp = TopExp_Explorer(forma, TopAbs_FACE)
    idx = 0
    while exp.More():
        cara = TopoDS.Face(exp.Current())
        info = geometria_cara(cara, idx)

        loc = TopLoc_Location()
        tri = BRep_Tool.Triangulation_s(cara, loc)
        n_tri = 0
        if tri is not None:
            trsf = loc.Transformation()
            base = len(posiciones) // 3
            invertida = cara.Orientation() == TopAbs_REVERSED
            nn = tri.NbNodes()
            if not tri.HasNormals():
                from OCP.BRepLib import BRepLib_ToolTriangulatedShape
                BRepLib_ToolTriangulatedShape.ComputeNormals_s(cara, tri)
            for i in range(1, nn + 1):
                p = tri.Node(i).Transformed(trsf)
                posiciones += [p.X(), p.Y(), p.Z()]
                n = tri.Normal(i).Transformed(trsf)
                s = -1.0 if invertida else 1.0
                normales += [s * n.X(), s * n.Y(), s * n.Z()]
            for j in range(1, tri.NbTriangles() + 1):
                a, b, c = tri.Triangle(j).Get()
                if invertida:
                    b, c = c, b
                indices += [base + a - 1, base + b - 1, base + c - 1]
                n_tri += 1

        info["tri_inicio"] = tri_total
        info["tri_cuenta"] = n_tri
        tri_total += n_tri
        caras.append(info)
        idx += 1
        exp.Next()

    if not indices:
        raise ValueError("No se pudo generar la malla del modelo")

    glb = _a_glb(posiciones, normales, indices)
    resultado = {
        "version": 1,
        "unidades": "mm",
        "caja": caja,
        "num_caras": len(caras),
        "num_triangulos": tri_total,
        "caras": caras,
    }
    return glb, resultado


def _a_glb(pos, nor, idx):
    """Escribe un .glb mínimo (1 malla, posiciones + normales + índices)."""
    n_vert = len(pos) // 3
    b_pos = struct.pack(f"<{len(pos)}f", *pos)
    b_nor = struct.pack(f"<{len(nor)}f", *nor)
    b_idx = struct.pack(f"<{len(idx)}I", *idx)
    xs, ys, zs = pos[0::3], pos[1::3], pos[2::3]

    def pad4(b, rell=b"\x00"):
        return b + rell * ((4 - len(b) % 4) % 4)

    binario = pad4(b_pos) + pad4(b_nor) + pad4(b_idx)
    o_nor = len(pad4(b_pos))
    o_idx = o_nor + len(pad4(b_nor))

    gltf = {
        "asset": {"version": "2.0", "generator": "dinmec-geometria"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{
            "attributes": {"POSITION": 0, "NORMAL": 1}, "indices": 2, "mode": 4}]}],
        "buffers": [{"byteLength": len(binario)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": 0, "byteLength": len(b_pos), "target": 34962},
            {"buffer": 0, "byteOffset": o_nor, "byteLength": len(b_nor), "target": 34962},
            {"buffer": 0, "byteOffset": o_idx, "byteLength": len(b_idx), "target": 34963},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": n_vert, "type": "VEC3",
             "min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]},
            {"bufferView": 1, "componentType": 5126, "count": n_vert, "type": "VEC3"},
            {"bufferView": 2, "componentType": 5125, "count": len(idx), "type": "SCALAR"},
        ],
    }
    j = pad4(json.dumps(gltf, separators=(",", ":")).encode(), b" ")
    total = 12 + 8 + len(j) + 8 + len(binario)
    return (struct.pack("<III", 0x46546C67, 2, total)
            + struct.pack("<II", len(j), 0x4E4F534A) + j
            + struct.pack("<II", len(binario), 0x004E4942) + binario)
