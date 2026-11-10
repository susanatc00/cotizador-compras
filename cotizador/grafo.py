"""Construcción y compilación del grafo LangGraph.

    START → interpretar_solicitud → validar_solicitud
    validar_solicitud ─(faltan datos, modo interactivo)→ pedir_aclaracion ─(respuesta)→ interpretar_solicitud
    validar_solicitud ─(completo / sin interacción)→ normalizar_solicitud
    normalizar_solicitud ─(Send × proveedor)→ consultar_proveedor   (en paralelo)
    consultar_proveedor → comparar_ofertas → armar_presupuesto → redactar_recomendacion → generar_reporte → END
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from langchain_core.runnables import Runnable
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from cotizador import nodos
from cotizador.proveedores import Registro, cargar_registro
from cotizador.schemas import EstadoCotizacion


def despachar_consultas(state: EstadoCotizacion):
    """Arista condicional que reparte la consulta: un `Send` por proveedor."""
    if not state.solicitud.items:
        return "generar_reporte"
    items = [{"idx": it.idx, "termino_busqueda": it.termino_busqueda} for it in state.solicitud.items]
    return [Send("consultar_proveedor", {"proveedor_id": pid, "items": items})
            for pid in state.solicitud.proveedores_ids]


def construir_grafo(
    interprete: Optional[Runnable] = None,
    juez: Optional[Runnable] = None,
    redactor: Optional[Runnable] = None,
    registro: Optional[Registro] = None,
    permitir_sustitutos: bool = True,
    dir_salida: Optional[Path] = None,
    checkpointer=None,
):
    """Arma y compila el grafo. Las dependencias se inyectan (LLM o reglas, web o catálogo)."""
    if interprete is None or juez is None or redactor is None:
        from cotizador.llm import construir_interprete, construir_juez, construir_redactor, crear_modelo

        modelo = crear_modelo()
        interprete = interprete or construir_interprete(modelo)
        juez = juez or construir_juez(modelo)
        redactor = redactor or construir_redactor(modelo)
    registro = registro or cargar_registro()

    g = StateGraph(EstadoCotizacion)
    g.add_node("interpretar_solicitud", nodos.crear_nodo_interpretar(interprete))
    g.add_node("validar_solicitud", nodos.validar_solicitud)
    g.add_node("pedir_aclaracion", nodos.pedir_aclaracion)
    g.add_node("normalizar_solicitud", nodos.crear_nodo_normalizar(registro))
    g.add_node("consultar_proveedor", nodos.crear_nodo_consultar(registro))
    g.add_node("comparar_ofertas", nodos.crear_nodo_comparar(juez))
    g.add_node("armar_presupuesto", nodos.crear_nodo_presupuestar(registro, permitir_sustitutos))
    g.add_node("redactar_recomendacion", nodos.crear_nodo_redactar(redactor))
    g.add_node("generar_reporte", nodos.crear_nodo_reporte(dir_salida))

    g.add_edge(START, "interpretar_solicitud")
    g.add_edge("interpretar_solicitud", "validar_solicitud")
    g.add_conditional_edges("validar_solicitud", nodos.ruta_validacion,
                            {"pedir_aclaracion": "pedir_aclaracion", "normalizar_solicitud": "normalizar_solicitud"})
    g.add_conditional_edges("pedir_aclaracion", nodos.ruta_aclaracion,
                            {"interpretar_solicitud": "interpretar_solicitud",
                             "normalizar_solicitud": "normalizar_solicitud"})
    g.add_conditional_edges("normalizar_solicitud", despachar_consultas,
                            ["consultar_proveedor", "generar_reporte"])
    g.add_edge("consultar_proveedor", "comparar_ofertas")
    g.add_edge("comparar_ofertas", "armar_presupuesto")
    g.add_edge("armar_presupuesto", "redactar_recomendacion")
    g.add_edge("redactar_recomendacion", "generar_reporte")
    g.add_edge("generar_reporte", END)

    return g.compile(checkpointer=checkpointer or crear_checkpointer())


# Tipos propios que el checkpointer puede reconstruir al reanudar tras interrupt().
_TIPOS_ESTADO = ["EstadoCotizacion", "SolicitudInterpretada", "ItemInterpretado", "Solicitud", "ItemSolicitado",
                 "ResultadoConsulta", "Oferta", "OfertaEvaluada", "Presentacion", "PresupuestoOpcion",
                 "LineaPresupuesto", "CostoAdicional", "Recomendacion"]


def crear_checkpointer() -> InMemorySaver:
    serde = JsonPlusSerializer(allowed_msgpack_modules=[("cotizador.schemas", t) for t in _TIPOS_ESTADO])
    return InMemorySaver(serde=serde)
