"""Componentes por reglas, intercambiables con los componentes LLM.

Se usan en el modo `--sin-llm` (sin OPENAI_API_KEY) y en las pruebas. Son más
limitados que el LLM, pero el grafo no cambia: solo cambia el Runnable inyectado.
"""
from __future__ import annotations

import json
import re

from langchain_core.runnables import RunnableLambda

from cotizador.schemas import ItemInterpretado, SolicitudInterpretada, VeredictoOferta, VeredictosItem
from cotizador.texto import contiene_palabra, cop, normalizar

_NUMEROS = {"un": 1, "una": 1, "uno": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6,
            "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "doce": 12, "media": 0.5, "medio": 0.5}

_UNIDADES = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogramo": "kg", "kilogramos": "kg",
    "g": "g", "gr": "g", "gramos": "g",
    "lb": "lb", "libra": "lb", "libras": "lb",
    "l": "L", "lt": "L", "litro": "L", "litros": "L",
    "ml": "mL", "mililitros": "mL",
    "und": "und", "unidad": "und", "unidades": "und", "paquete": "und", "paquetes": "und",
    "botella": "und", "botellas": "und", "bolsa": "und", "bolsas": "und", "docena": "und", "docenas": "und",
}

_RE_ITEM = re.compile(
    r"(?P<num>\d+(?:[.,]\d+)?|" + "|".join(_NUMEROS) + r")\s*"
    r"(?P<uni>" + "|".join(sorted(_UNIDADES, key=len, reverse=True)) + r")?\s+"
    r"(?:de\s+)?(?P<prod>[a-zñ ]+?)(?=\s*(?:,|\by\b|\.|;|$))"
)
_PALABRAS_CORTE = {"consulta", "consultar", "prepara", "para", "en", "cotiza", "compara", "busca"}
_NO_PRODUCTOS = {"supermercados", "tiendas", "proveedores", "presupuesto", "cotizacion", "favor", "vez"}
_RE_CIUDAD = re.compile(r"\b(?:para|en)\s+(?P<c>[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+(?:\s+[A-ZÁÉÍÓÚÑ][\wáéíóúñ]+)*)")
_RE_CANT_PROV = re.compile(r"\b(?P<n>\d+|dos|tres|cuatro)\s+(?:supermercados|tiendas|proveedores|almacenes)")
_RE_MARCA = re.compile(r"\bmarca\s+(?P<m>[a-z0-9ñ]+)")

# Palabras que convierten un producto en otra variedad (sustituto) si el usuario no las pidió.
MODIFICADORES_SUSTITUTO = {
    "integral", "parbolizado", "jazmin", "basmati", "sushi", "tailandes", "oliva", "coco", "spray",
    "morena", "mascabado", "light", "endulzante", "deslactosada", "deslactosado", "descremada",
    "semidescremada", "saborizada", "sabor", "organico", "organica", "dorada", "sin",
}
_STOP = {"de", "del", "la", "el", "en", "con", "para", "x", "y"}


def interpretar_por_reglas(texto: str, nombres_tiendas: list[str] | None = None) -> SolicitudInterpretada:
    t = normalizar(texto.replace("\n", ". "))
    items: list[ItemInterpretado] = []
    for m in _RE_ITEM.finditer(t):
        prod = re.split(r"\b(?:" + "|".join(_PALABRAS_CORTE) + r")\b", m.group("prod"))[0].strip()
        if not prod or prod in _UNIDADES or prod.split()[0] in _NO_PRODUCTOS:
            continue
        num = m.group("num")
        cantidad = _NUMEROS.get(num) or float(num.replace(",", "."))
        uni_txt = m.group("uni")
        unidad = _UNIDADES.get(uni_txt) if uni_txt else None
        if uni_txt in {"docena", "docenas"}:
            cantidad *= 12
        marca = None
        mm = _RE_MARCA.search(prod)
        if mm:
            marca = mm.group("m")
            prod = prod[: mm.start()].strip()
        palabras = [w for w in prod.split() if w not in _STOP]
        if not palabras:
            continue
        items.append(
            ItemInterpretado(
                texto_original=m.group(0).strip(), producto=prod, termino_busqueda=palabras[0],
                cantidad=cantidad, unidad=unidad, especificaciones=palabras[1:], marca=marca,
                marca_obligatoria=marca is not None, presentacion_cantidad=None, presentacion_unidad=None,
                presentacion_obligatoria=False,
            )
        )
    ciudad = None
    tiendas_norm = {normalizar(n) for n in (nombres_tiendas or [])}
    for mc in _RE_CIUDAD.finditer(texto):
        if normalizar(mc.group("c")) not in tiendas_norm:
            ciudad = mc.group("c")
            break
    mp = _RE_CANT_PROV.search(t)
    cant_prov = None
    if mp:
        cant_prov = _NUMEROS.get(mp.group("n")) or int(mp.group("n"))
    mencionados = [n for n in (nombres_tiendas or []) if contiene_palabra(t, n)]
    return SolicitudInterpretada(items=items, ciudad=ciudad, proveedores_mencionados=mencionados,
                                 cantidad_proveedores=cant_prov, observaciones=[])


def juzgar_por_reglas(articulo: dict, ofertas: list[dict]) -> VeredictosItem:
    """Reglas: el sustantivo del producto debe estar entre las 2 primeras palabras
    del nombre; las especificaciones pedidas deben aparecer; un modificador de
    variedad no pedido lo vuelve sustituto."""
    base = normalizar(articulo["termino_busqueda"])
    specs = [normalizar(s) for s in articulo.get("especificaciones", [])]
    veredictos = []
    for o in ofertas:
        nombre = normalizar(o["nombre"])
        primeras = " ".join(nombre.split()[:2])
        if not contiene_palabra(primeras, base):
            veredictos.append(VeredictoOferta(id=o["id"], categoria="no_corresponde",
                                              motivo=f"'{base}' no es el producto principal del nombre"))
            continue
        if re.search(rf"(?<![a-z]){re.escape(base)}e?s? con ", nombre):
            veredictos.append(VeredictoOferta(id=o["id"], categoria="no_corresponde",
                                              motivo=f"preparación con '{base}', no el producto"))
            continue
        faltan = [s for s in specs if not all(contiene_palabra(nombre, w) for w in s.split())]
        extra = sorted(w for w in MODIFICADORES_SUSTITUTO if contiene_palabra(nombre, w)
                       and not any(w in s.split() for s in specs))
        if faltan:
            veredictos.append(VeredictoOferta(id=o["id"], categoria="sustituto",
                                              motivo=f"no indica: {', '.join(faltan)}"))
        elif extra:
            veredictos.append(VeredictoOferta(id=o["id"], categoria="sustituto",
                                              motivo=f"otra variedad: {', '.join(extra)}"))
        else:
            veredictos.append(VeredictoOferta(id=o["id"], categoria="equivalente", motivo="mismo producto"))
    return VeredictosItem(veredictos=veredictos)


def redactar_por_reglas(resumen: dict) -> str:
    rec = resumen.get("recomendacion") or {}
    if not rec.get("opcion"):
        return ("No fue posible recomendar una compra: ninguna opción cubre los productos solicitados con "
                "ofertas comparables. Revise la tabla de productos sin cotización.")
    viables = [o for o in resumen.get("opciones", []) if o.get("viable")]
    elegida = next((o for o in viables if o["nombre"] == rec["opcion"]), None)
    partes = []
    estimado = bool(elegida and elegida.get("total_estimado_con_domicilio_COP") is not None)
    clave = "total_estimado_con_domicilio_COP" if estimado else "total_conocido_COP"
    if elegida:
        otras = [f"{o['nombre']} ({cop(o[clave])})" for o in viables if o is not elegida and o.get(clave) is not None]
        que = "un total estimado, con domicilio," if estimado else "un subtotal de productos"
        partes.append(f"{rec['opcion']} tiene {que} de {cop(elegida[clave])}"
                      + (f", frente a {', '.join(otras)}." if otras else "."))
    if resumen.get("sin_cotizacion"):
        partes.append("Productos sin cotización: " + ", ".join(resumen["sin_cotizacion"]) + ".")
    if estimado:
        partes.append("El domicilio es un valor estimado: el total final no está confirmado hasta que la tienda "
                      "calcule el envío al hacer el pedido.")
    else:
        partes.append("El total final no está confirmado mientras el costo de envío sea desconocido; "
                      "verifíquelo en la tienda antes de comprar.")
    return " ".join(partes)


def interprete_reglas(nombres_tiendas: list[str] | None = None):
    return RunnableLambda(lambda x: interpretar_por_reglas(x["texto_usuario"], nombres_tiendas))


def juez_reglas():
    return RunnableLambda(lambda x: juzgar_por_reglas(json.loads(x["articulo"]), json.loads(x["ofertas"])))


def redactor_reglas():
    return RunnableLambda(lambda x: redactar_por_reglas(json.loads(x["resumen"])))
