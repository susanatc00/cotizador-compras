"""Normalización y comparación de ofertas (código determinista, sin LLM).

Combina el veredicto semántico (LLM o reglas: equivalente / sustituto /
no_corresponde) con verificaciones programáticas:

    no_corresponde                                  -> DESCARTADO
    sin precio, moneda distinta de COP,
    contenido ilegible o unidad no convertible      -> NO COMPARABLE
    incumple marca o presentación obligatoria        -> (equivalente|sustituto) pero NO elegible
    agotado                                          -> (equivalente|sustituto) pero NO elegible
    resto                                            -> equivalente | sustituto, elegible

Para las ofertas comparables calcula precio por unidad base (COP/kg, COP/L,
COP/und), empaques necesarios (redondeo hacia arriba), costo y excedente.
"""
from __future__ import annotations

import math

from cotizador.schemas import ItemSolicitado, Oferta, OfertaEvaluada, VeredictoOferta
from cotizador.texto import contiene_palabra, normalizar
from cotizador.unidades import UNIDAD_BASE, cantidad_en_dimension, fmt_cantidad, leer_presentacion

MONEDA = "COP"
TOLERANCIA_PRESENTACION = 0.02  # 2 %


def _cumple_marca(oferta: Oferta, marca: str) -> bool:
    m = normalizar(marca)
    return contiene_palabra(normalizar(oferta.marca), m) or contiene_palabra(normalizar(oferta.nombre), m)


def _cumple_presentacion(cantidad_empaque: float, unidades: int | None, requerida: float) -> bool:
    candidatos = [cantidad_empaque]
    if unidades:
        candidatos.append(cantidad_empaque / unidades)
    return any(abs(c - requerida) <= TOLERANCIA_PRESENTACION * requerida for c in candidatos)


def evaluar_oferta(item: ItemSolicitado, oferta: Oferta, veredicto: VeredictoOferta | None) -> OfertaEvaluada:
    motivos: list[str] = []
    categoria = veredicto.categoria if veredicto else "no_corresponde"
    if veredicto:
        motivos.append(veredicto.motivo)
    else:
        motivos.append("sin veredicto de equivalencia")

    pres = leer_presentacion(oferta.presentacion_texto or oferta.nombre)
    ev = OfertaEvaluada(item_idx=item.idx, oferta=oferta, clasificacion="descartado", elegible=False,
                        motivos=motivos, presentacion=pres)
    motivos = ev.motivos  # Pydantic copia la lista: desde aquí se agrega sobre la del modelo

    if categoria == "no_corresponde":
        return ev

    # --- ¿Hay información suficiente para comparar? ---
    if oferta.precio is None:
        ev.clasificacion = "no_comparable"
        motivos.append("la fuente no publica precio")
        return ev
    if (oferta.moneda or "").upper() != MONEDA:
        ev.clasificacion = "no_comparable"
        motivos.append(f"moneda {oferta.moneda or 'desconocida'} (se requiere {MONEDA})")
        return ev
    cantidad, suposicion = cantidad_en_dimension(pres, item.dimension)
    if cantidad is None or cantidad <= 0:
        ev.clasificacion = "no_comparable"
        if pres.dimension is None:
            motivos.append("no se pudo leer el contenido del empaque")
        else:
            motivos.append(f"el empaque está en {UNIDAD_BASE[pres.dimension]} y se pidió en {item.unidad_base}")
        return ev

    ev.clasificacion = categoria
    ev.elegible = True
    if suposicion:
        motivos.append(suposicion)
    if pres.inferida:
        motivos.append(f"presentación inferida del nombre: {pres.texto}")

    # --- Restricciones del usuario ---
    if item.marca:
        if not _cumple_marca(oferta, item.marca):
            if item.marca_obligatoria:
                ev.elegible = False
                motivos.append(f"incumple la marca exigida ({item.marca})")
            else:
                motivos.append(f"marca distinta a la preferida ({item.marca})")
        else:
            motivos.append(f"marca {item.marca}")
    if item.presentacion_base:
        ok = _cumple_presentacion(cantidad, pres.unidades_por_empaque, item.presentacion_base)
        if not ok:
            texto = fmt_cantidad(item.presentacion_base, item.unidad_base)
            if item.presentacion_obligatoria:
                ev.elegible = False
                motivos.append(f"incumple la presentación exigida ({texto})")
            else:
                motivos.append(f"presentación distinta a la sugerida ({texto})")
    if oferta.disponibilidad == "agotado":
        ev.elegible = False
        motivos.append("agotado según la fuente")
    elif oferta.disponibilidad == "desconocida":
        motivos.append("disponibilidad no publicada")

    # --- Cálculos ---
    ev.precio_unitario = oferta.precio / cantidad
    ev.empaques = max(1, math.ceil(item.cantidad / cantidad - 1e-9))
    ev.costo = ev.empaques * oferta.precio
    ev.excedente = round(ev.empaques * cantidad - item.cantidad, 6)
    return ev


def evaluar_item(item: ItemSolicitado, ofertas: list[Oferta], veredictos: list[VeredictoOferta]) -> list[OfertaEvaluada]:
    por_id = {v.id: v for v in veredictos}
    evaluadas = [evaluar_oferta(item, o, por_id.get(o.id)) for o in ofertas]
    orden = {"equivalente": 0, "sustituto": 1, "no_comparable": 2, "descartado": 3}
    evaluadas.sort(key=lambda e: (orden[e.clasificacion], not e.elegible,
                                  e.costo if e.costo is not None else math.inf))
    return evaluadas
