"""Nodos del grafo.

Cada fábrica `crear_nodo_*` recibe sus dependencias (LLM, registro de proveedores…)
y devuelve la función-nodo. Así el mismo grafo corre con OpenAI y la web en
producción, y con reglas y catálogos locales en las pruebas.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

from langchain_core.runnables import Runnable
from langgraph.types import interrupt

from cotizador import reglas
from cotizador.comparacion import evaluar_item
from cotizador.presupuesto import armar_presupuestos, recomendar
from cotizador.proveedores import Registro, seleccionar_proveedores
from cotizador.reporte import generar_reporte, guardar_salidas
from cotizador.schemas import (
    EstadoCotizacion,
    ItemSolicitado,
    ResultadoConsulta,
    Solicitud,
    SolicitudInterpretada,
    VeredictosItem,
)
from cotizador.unidades import fmt_cantidad, unidad_solicitud_a_base

MAX_RONDAS_ACLARACION = 2


# ---------------------------------------------------------------------------
# 1. Interpretar (LLM)
# ---------------------------------------------------------------------------
def crear_nodo_interpretar(interprete: Runnable):
    def interpretar_solicitud(state: EstadoCotizacion) -> dict:
        interp: SolicitudInterpretada = interprete.invoke({"texto_usuario": state.texto_usuario})
        resumen = "; ".join(
            f"{i.producto} ({i.cantidad if i.cantidad is not None else '?'} {i.unidad or '?'})" for i in interp.items
        )
        return {
            "interpretacion": interp,
            "historial": [f"[interpretar_solicitud] {len(interp.items)} artículos: {resumen}; "
                          f"ciudad={interp.ciudad or '?'}"],
        }

    return interpretar_solicitud


# ---------------------------------------------------------------------------
# 2. Validar datos imprescindibles (código)
# ---------------------------------------------------------------------------
def validar_solicitud(state: EstadoCotizacion) -> dict:
    interp = state.interpretacion
    faltantes: list[str] = []
    if not interp.items:
        faltantes.append("No identifiqué ningún producto. ¿Qué artículos necesita y en qué cantidad?")
    for it in interp.items:
        if it.cantidad is None and it.unidad is None:
            faltantes.append(f"¿Qué cantidad de {it.producto} necesita (por ejemplo 2 kg, 1 L o 3 unidades)?")
        elif it.cantidad is None:
            faltantes.append(f"¿Cuántos {it.unidad} de {it.producto} necesita?")
        elif it.unidad is None:
            faltantes.append(f"¿En qué unidad son las {it.cantidad:g} de {it.producto} (kg, L, unidades…)?")
    if not interp.ciudad:
        faltantes.append("¿En qué ciudad hará la compra?")
    return {
        "faltantes": faltantes,
        "historial": [f"[validar_solicitud] {len(faltantes)} datos faltantes"],
    }


def ruta_validacion(state: EstadoCotizacion) -> str:
    if not state.faltantes:
        return "normalizar_solicitud"
    if state.modo_interactivo and state.rondas_aclaracion < MAX_RONDAS_ACLARACION:
        return "pedir_aclaracion"
    return "normalizar_solicitud"  # registra suposiciones explícitas


# ---------------------------------------------------------------------------
# 3. Pedir aclaración (human-in-the-loop con interrupt)
# ---------------------------------------------------------------------------
def pedir_aclaracion(state: EstadoCotizacion) -> dict:
    respuesta = interrupt({"preguntas": state.faltantes})
    respuesta = str(respuesta or "").strip()
    if not respuesta:
        # El usuario no quiso aclarar: se continúa con suposiciones.
        return {"rondas_aclaracion": MAX_RONDAS_ACLARACION, "faltantes": [],
                "historial": ["[pedir_aclaracion] sin respuesta; se usarán suposiciones"]}
    return {
        "texto_usuario": f"{state.texto_usuario}\nAclaración del usuario: {respuesta}",
        "rondas_aclaracion": state.rondas_aclaracion + 1,
        "historial": [f"[pedir_aclaracion] respuesta recibida: {respuesta}"],
    }


def ruta_aclaracion(state: EstadoCotizacion) -> str:
    """Con respuesta -> se vuelve a interpretar; sin respuesta -> se sigue con suposiciones."""
    return "interpretar_solicitud" if state.faltantes else "normalizar_solicitud"


# ---------------------------------------------------------------------------
# 4. Normalizar solicitud + suposiciones + selección de proveedores (código)
# ---------------------------------------------------------------------------
def crear_nodo_normalizar(registro: Registro):
    def normalizar_solicitud(state: EstadoCotizacion) -> dict:
        interp = state.interpretacion
        suposiciones: list[str] = []
        items: list[ItemSolicitado] = []
        for idx, it in enumerate(interp.items):
            cantidad, unidad = it.cantidad, it.unidad
            if cantidad is None and unidad is None:
                cantidad, unidad = 1, "und"
                suposiciones.append(f"{it.producto}: no se indicó cantidad; se cotiza 1 empaque.")
            elif cantidad is None:
                cantidad = 1
                suposiciones.append(f"{it.producto}: no se indicó cantidad; se asume 1 {unidad}.")
            elif unidad is None:
                unidad = "und"
                suposiciones.append(f"{it.producto}: no se indicó unidad; se asumen {cantidad:g} unidades/empaques.")
            if unidad == "lb":
                suposiciones.append(f"{it.producto}: 1 libra se toma como 500 g (uso comercial en Colombia).")
            base, dim, ub = unidad_solicitud_a_base(cantidad, unidad)
            pres_base = None
            if it.presentacion_cantidad and it.presentacion_unidad:
                pb, pdim, _ = unidad_solicitud_a_base(it.presentacion_cantidad, it.presentacion_unidad)
                pres_base = pb if pdim == dim else None
            items.append(
                ItemSolicitado(
                    idx=idx, texto_original=it.texto_original, producto=it.producto,
                    termino_busqueda=it.termino_busqueda, cantidad=base, dimension=dim, unidad_base=ub,
                    cantidad_texto=fmt_cantidad(base, ub), especificaciones=it.especificaciones,
                    marca=it.marca, marca_obligatoria=it.marca_obligatoria,
                    presentacion_base=pres_base, presentacion_obligatoria=it.presentacion_obligatoria and bool(pres_base),
                )
            )
        ciudad = interp.ciudad
        if not ciudad:
            ciudad = registro.ciudad_por_defecto
            suposiciones.append(f"No se indicó ciudad; se asume {ciudad}.")
        ids, sup_prov = seleccionar_proveedores(registro, interp.proveedores_mencionados, interp.cantidad_proveedores)
        suposiciones += sup_prov
        condiciones = []
        for pid in ids:
            condiciones += registro.proveedores[pid].condiciones(ciudad)
        return {
            "solicitud": Solicitud(items=items, ciudad=ciudad, proveedores_ids=ids),
            "suposiciones": suposiciones,
            "condiciones_no_verificadas": condiciones,
            "historial": [f"[normalizar_solicitud] {len(items)} artículos; proveedores={ids}; "
                          f"{len(suposiciones)} suposiciones"],
        }

    return normalizar_solicitud


# ---------------------------------------------------------------------------
# 5. Consultar proveedores en paralelo (Send) — herramientas programáticas
# ---------------------------------------------------------------------------
class ConsultaProveedor(TypedDict):
    proveedor_id: str
    items: list[dict]


def crear_nodo_consultar(registro: Registro):
    def consultar_proveedor(payload: ConsultaProveedor) -> dict:
        prov = registro.proveedores[payload["proveedor_id"]]
        resultados: list[ResultadoConsulta] = []
        for it in payload["items"]:
            resultados.append(prov.buscar(it["termino_busqueda"], it["idx"]))
        resumen = ", ".join(f"{r.termino}:{r.estado}({len(r.ofertas)})" for r in resultados)
        return {"resultados_consulta": resultados,
                "historial": [f"[consultar_proveedor:{prov.id}] {resumen}"]}

    return consultar_proveedor


# ---------------------------------------------------------------------------
# 6. Comparar ofertas: juez semántico (LLM) + normalización (código)
# ---------------------------------------------------------------------------
def crear_nodo_comparar(juez: Runnable):
    def comparar_ofertas(state: EstadoCotizacion) -> dict:
        items = state.solicitud.items
        entradas, ofertas_por_item = [], []
        for it in items:
            vistas, ofertas = set(), []
            for r in state.resultados_consulta:
                if r.item_idx != it.idx:
                    continue
                for o in r.ofertas:
                    if o.id not in vistas:
                        vistas.add(o.id)
                        ofertas.append(o)
            ofertas_por_item.append(ofertas)
            articulo = {"producto": it.producto, "termino_busqueda": it.termino_busqueda,
                        "especificaciones": it.especificaciones, "marca": it.marca}
            entradas.append({
                "articulo": json.dumps(articulo, ensure_ascii=False),
                "ofertas": json.dumps([{"id": o.id, "nombre": o.nombre, "marca": o.marca} for o in ofertas],
                                      ensure_ascii=False),
            })
        historial, suposiciones = [], []
        con_ofertas = [i for i, ofs in enumerate(ofertas_por_item) if ofs]
        veredictos: dict[int, VeredictosItem] = {}
        try:
            salidas = juez.batch([entradas[i] for i in con_ofertas])
            veredictos = dict(zip(con_ofertas, salidas))
        except Exception as e:  # noqa: BLE001 — el LLM puede fallar por red/cuota
            historial.append(f"[comparar_ofertas] juez LLM falló ({type(e).__name__}); se usan reglas")
            suposiciones.append("La clasificación equivalente/sustituto se hizo con reglas porque el LLM no respondió.")
            reglas_juez = reglas.juez_reglas()
            veredictos = {i: reglas_juez.invoke(entradas[i]) for i in con_ofertas}

        evaluadas = []
        for it, ofertas in zip(items, ofertas_por_item):
            v = veredictos.get(it.idx, VeredictosItem(veredictos=[]))
            evaluadas += evaluar_item(it, ofertas, v.veredictos)
        conteo = {}
        for e in evaluadas:
            conteo[e.clasificacion] = conteo.get(e.clasificacion, 0) + 1
        historial.append(f"[comparar_ofertas] {len(evaluadas)} ofertas evaluadas: {conteo}")
        return {"evaluadas": evaluadas, "historial": historial, "suposiciones": suposiciones}

    return comparar_ofertas


# ---------------------------------------------------------------------------
# 7. Presupuestar (código)
# ---------------------------------------------------------------------------
def crear_nodo_presupuestar(registro: Registro, permitir_sustitutos: bool = True,
                            envio_estimado: float | None = None):
    def armar_presupuesto(state: EstadoCotizacion) -> dict:
        ids = state.solicitud.proveedores_ids
        nombres = {pid: registro.proveedores[pid].nombre for pid in ids}
        costos = {pid: registro.proveedores[pid].costos_adicionales() for pid in ids}
        _, nota = registro.envio_estimado
        opciones = armar_presupuestos(state.solicitud.items, state.evaluadas, nombres, costos, permitir_sustitutos,
                                      envio_estimado=envio_estimado, nota_envio_estimado=nota)
        rec = recomendar(opciones)
        return {
            "opciones": opciones,
            "recomendacion": rec,
            "historial": [f"[armar_presupuesto] {len(opciones)} opciones; recomendada={rec.opcion}"],
        }

    return armar_presupuesto


# ---------------------------------------------------------------------------
# 8. Redactar recomendación (LLM, solo explica cifras ya calculadas)
# ---------------------------------------------------------------------------
def resumen_para_redactor(state: EstadoCotizacion) -> dict:
    rec = state.recomendacion
    elegida = next((o for o in state.opciones if rec and o.nombre == rec.opcion), None)
    diferencias = {}
    if elegida:
        for o in state.opciones:
            if o.viable and o is not elegida:
                diferencias[o.nombre] = round(o.total_conocido - elegida.total_conocido)
    return {
        "diferencia_total_conocido_vs_recomendada_COP": diferencias,
        "recomendacion": state.recomendacion.model_dump() if state.recomendacion else None,
        "opciones": [
            {
                "nombre": o.nombre, "tipo": o.tipo, "viable": o.viable, "cobertura": o.cobertura,
                "subtotal_productos_COP": round(o.subtotal_productos),
                "total_conocido_COP": round(o.total_conocido),
                "total_estimado_con_domicilio_COP": None if o.total_estimado is None else round(o.total_estimado),
                "total_final_confirmado_COP": None if o.total_final_confirmado is None else round(o.total_final_confirmado),
                "pendientes": o.pendientes, "nota": o.nota,
                "lineas": [
                    {"producto": l.producto, "cantidad": l.cantidad_texto,
                     "oferta": l.evaluada.oferta.nombre if l.evaluada else None,
                     "clasificacion": l.evaluada.clasificacion if l.evaluada else None,
                     "costo_COP": round(l.evaluada.costo) if l.evaluada else None}
                    for l in o.lineas
                ],
            }
            for o in state.opciones
        ],
        "sin_cotizacion": sorted({p for o in state.opciones if o.tipo == "combinada" for p in o.items_sin_cotizacion}),
        "condiciones_no_verificadas": state.condiciones_no_verificadas,
        "suposiciones": state.suposiciones,
    }


def crear_nodo_redactar(redactor: Runnable):
    def redactar_recomendacion(state: EstadoCotizacion) -> dict:
        resumen = resumen_para_redactor(state)
        historial = []
        try:
            texto = redactor.invoke({"resumen": json.dumps(resumen, ensure_ascii=False)})
        except Exception as e:  # noqa: BLE001
            historial.append(f"[redactar_recomendacion] LLM falló ({type(e).__name__}); texto por plantilla")
            texto = reglas.redactar_por_reglas(resumen)
        rec = state.recomendacion.model_copy(update={"texto": texto.strip()})
        historial.append("[redactar_recomendacion] explicación generada")
        return {"recomendacion": rec, "historial": historial}

    return redactar_recomendacion


# ---------------------------------------------------------------------------
# 9. Reporte (código)
# ---------------------------------------------------------------------------
def crear_nodo_reporte(dir_salida: Path | None):
    def generar_reporte_nodo(state: EstadoCotizacion) -> dict:
        md = generar_reporte(state)
        archivos = guardar_salidas(state, md, dir_salida) if dir_salida else []
        return {"reporte_md": md, "archivos_salida": archivos,
                "historial": [f"[generar_reporte] {len(md.splitlines())} líneas" +
                              (f"; guardado en {archivos[0]}" if archivos else "")]}

    return generar_reporte_nodo
