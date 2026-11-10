"""Interfaz web (Streamlit) del cotizador.

    uv run streamlit run app.py

Usa el mismo grafo que main.py. El grafo y su checkpointer se guardan en la
sesión para poder reanudar después de una aclaración (interrupt).
"""
from __future__ import annotations

import json
import uuid

import pandas as pd
import streamlit as st
from langgraph.types import Command

from cotizador import reglas
from cotizador.config import hay_api_key
from cotizador.grafo import construir_grafo
from cotizador.proveedores import cargar_registro
from cotizador.schemas import EstadoCotizacion
from cotizador.texto import cop

st.set_page_config(page_title="Cotizador de mercado", page_icon="🛒", layout="wide")

EJEMPLOS = {
    "Enunciado": "Necesito 5 kg de arroz, 2 litros de aceite y 3 kg de azúcar. Consulta dos supermercados y "
                 "prepara un presupuesto para Medellín.",
    "Con restricciones": "Quiero 3 kg de arroz marca Diana, 1 litro de aceite de girasol, una docena de huevos, "
                         "2 libras de lenteja y un detergente líquido. Cotiza en Éxito y Jumbo.",
    "Incompleta (pide aclaración)": "Necesito arroz y aceite para la casa, cotiza en Olímpica",
}

NOMBRES_NODO = {
    "interpretar_solicitud": "Interpretando la solicitud (LLM)",
    "validar_solicitud": "Validando datos",
    "pedir_aclaracion": "Aclaración recibida",
    "normalizar_solicitud": "Normalizando unidades y eligiendo proveedores",
    "consultar_proveedor": "Consultando proveedor",
    "comparar_ofertas": "Comparando ofertas (LLM + reglas)",
    "armar_presupuesto": "Armando presupuestos",
    "redactar_recomendacion": "Redactando la recomendación (LLM)",
    "generar_reporte": "Generando el reporte",
}

ETIQUETA = {"equivalente": "Equivalente", "sustituto": "Sustituto", "no_comparable": "No comparable",
            "descartado": "Descartado"}
COBERTURA = {"completa": "Completa", "completa_con_sustitutos": "Completa con sustitutos", "parcial": "Parcial",
             "nula": "Sin cobertura"}


# --------------------------------------------------------------------------- estado de sesión
ss = st.session_state
ss.setdefault("texto", EJEMPLOS["Enunciado"])
ss.setdefault("grafo", None)
ss.setdefault("config", None)
ss.setdefault("preguntas", None)
ss.setdefault("estado", None)
ss.setdefault("pasos", [])


def construir(fuente: str, usar_llm: bool, sustitutos: bool):
    registro = cargar_registro(fuente)
    kwargs = {}
    if not usar_llm:
        alias = [a for p in registro.proveedores.values() for a in [p.nombre, *p.alias]]
        alias += [a for info in registro.no_soportados.values() for a in info.get("alias", [])]
        kwargs = dict(interprete=reglas.interprete_reglas(alias), juez=reglas.juez_reglas(),
                      redactor=reglas.redactor_reglas())
    return construir_grafo(registro=registro, permitir_sustitutos=sustitutos, dir_salida=None, **kwargs)


def ejecutar(entrada) -> None:
    """Corre el grafo mostrando el avance; deja en sesión el estado final o las preguntas."""
    grafo, config = ss.grafo, ss.config
    with st.status("Cotizando…", expanded=True) as estado_ui:
        for evento in grafo.stream(entrada, config, stream_mode="updates"):
            for nodo, cambios in evento.items():
                if nodo == "__interrupt__":
                    continue
                detalle = ""
                if isinstance(cambios, dict) and cambios.get("historial"):
                    detalle = cambios["historial"][-1].split("] ", 1)[-1]
                linea = f"**{NOMBRES_NODO.get(nodo, nodo)}** — {detalle}"
                ss.pasos.append(linea)
                st.write(linea)
        snapshot = grafo.get_state(config)
        if snapshot.next:  # se detuvo en interrupt()
            ss.preguntas = snapshot.tasks[0].interrupts[0].value["preguntas"]
            estado_ui.update(label="Faltan datos: responda abajo", state="running")
        else:
            ss.preguntas = None
            ss.estado = EstadoCotizacion.model_validate(snapshot.values)
            estado_ui.update(label="Cotización lista", state="complete", expanded=False)


# --------------------------------------------------------------------------- barra lateral
with st.sidebar:
    st.header("Opciones")
    tiene_key = hay_api_key()
    usar_llm = st.toggle("Usar LLM (OpenAI)", value=tiene_key, disabled=not tiene_key,
                         help="Sin OPENAI_API_KEY en .env se usan las reglas de respaldo.")
    fuente = st.radio("Fuente de precios", ["auto", "web", "catalogo"], index=0,
                      format_func={"auto": "Web (con respaldo en catálogo)", "web": "Solo web en vivo",
                                   "catalogo": "Catálogo de referencia (sin internet)"}.get)
    sustitutos = st.checkbox("Permitir sustitutos en el presupuesto", value=True)
    interactivo = st.checkbox("Pedir aclaración si faltan datos", value=True,
                              help="Si se desmarca, el sistema registra suposiciones explícitas.")
    st.divider()
    st.caption("Ejemplos")
    for nombre, texto in EJEMPLOS.items():
        if st.button(nombre, width="stretch"):
            ss.texto = texto
    st.divider()
    st.caption("Proveedores: Olímpica y Jumbo (datos JSON-LD públicos, respetando robots.txt).")

# --------------------------------------------------------------------------- entrada
st.title("🛒 Cotizador de compras de mercado")
st.write("Escriba lo que necesita comprar. El sistema consulta dos supermercados, compara presentaciones "
         "y arma el presupuesto con sus fuentes.")

texto = st.text_area("Solicitud", key="texto", height=90)
if st.button("Cotizar", type="primary", disabled=not texto.strip()):
    ss.grafo = construir(fuente, usar_llm, sustitutos)
    ss.config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    ss.estado, ss.preguntas, ss.pasos = None, None, []
    ejecutar(EstadoCotizacion(texto_usuario=texto, modo_interactivo=interactivo))

if ss.preguntas:
    with st.form("aclaracion"):
        st.warning("Necesito aclarar algunos datos:\n\n" + "\n".join(f"- {p}" for p in ss.preguntas))
        respuesta = st.text_input("Su respuesta (vacío = continuar con suposiciones)",
                                  placeholder="Ej.: 3 kg de arroz y 1 litro de aceite, en Bello")
        if st.form_submit_button("Continuar"):
            ejecutar(Command(resume=respuesta))
            st.rerun()

# --------------------------------------------------------------------------- resultados
est: EstadoCotizacion | None = ss.estado
if est is None:
    st.stop()

if not est.solicitud or not est.solicitud.items:
    st.error("No se identificó ningún producto en la solicitud.")
    st.stop()

rec = est.recomendacion
if rec and rec.opcion:
    st.success(f"### Recomendación: {rec.opcion}\n{rec.criterio}")
    if rec.texto:
        st.write(rec.texto)

viables = [o for o in est.opciones if o.viable]
cols = st.columns(len(viables) or 1)
for col, op in zip(cols, viables):
    titulo = op.nombre if op.tipo == "combinada" else f"Todo en {op.nombre}"
    col.metric(titulo, cop(op.subtotal_productos), help="Subtotal de productos")
    col.caption(f"Cobertura: **{COBERTURA[op.cobertura]}**  \nTotal final: "
                f"**{cop(op.total_final_confirmado) if op.total_final_confirmado is not None else 'No confirmado'}**")

if est.suposiciones:
    with st.expander(f"Suposiciones explícitas ({len(est.suposiciones)})"):
        for s in est.suposiciones:
            st.write(f"- {s}")

tab_pres, tab_ofertas, tab_sol, tab_cons, tab_rep, tab_traza = st.tabs(
    ["Presupuestos", "Ofertas encontradas", "Solicitud interpretada", "Consultas y fuentes", "Reporte completo",
     "Traza del grafo"])

with tab_pres:
    for op in est.opciones:
        st.subheader(f"Compra completa en {op.nombre}" if op.tipo == "proveedor_unico" else op.nombre)
        if not op.viable:
            st.info(op.nota)
            continue
        if op.nota:
            st.caption(op.nota)
        filas = []
        for l in op.lineas:
            e = l.evaluada
            filas.append({
                "Producto": l.producto, "Cantidad": l.cantidad_texto,
                "Oferta elegida": e.oferta.nombre if e else f"Sin cotización ({l.nota})",
                "Proveedor": e.oferta.proveedor if e else "—",
                "Tipo": ETIQUETA[e.clasificacion] if e else "—",
                "Precio empaque": cop(e.oferta.precio) if e else "—",
                "Empaques": e.empaques if e else None,
                "Costo": cop(e.costo) if e else "—",
                "Fuente": e.oferta.url if e else None,
            })
        st.dataframe(pd.DataFrame(filas), hide_index=True, width="stretch",
                     column_config={"Fuente": st.column_config.LinkColumn("Fuente", display_text="ver")})
        costos = [{"Concepto": "Subtotal de productos", "Valor": cop(op.subtotal_productos)}]
        costos += [{"Concepto": c.concepto, "Valor": cop(c.valor)} for c in op.costos_adicionales]
        costos += [{"Concepto": "Total con costos conocidos", "Valor": cop(op.total_conocido)},
                   {"Concepto": "Total final confirmado",
                    "Valor": cop(op.total_final_confirmado) if op.total_final_confirmado is not None else "No confirmado"}]
        st.table(pd.DataFrame(costos).set_index("Concepto"))
        if op.pendientes:
            st.caption("Pendiente: " + "; ".join(op.pendientes))

with tab_ofertas:
    mostrar = st.multiselect("Clasificación", list(ETIQUETA.values()),
                             default=["Equivalente", "Sustituto", "No comparable"])
    for it in est.solicitud.items:
        ev = [e for e in est.evaluadas if e.item_idx == it.idx and ETIQUETA[e.clasificacion] in mostrar]
        st.subheader(f"{it.producto} — {it.cantidad_texto}")
        if not ev:
            st.caption("Sin ofertas con esa clasificación.")
            continue
        st.dataframe(pd.DataFrame([{
            "Proveedor": e.oferta.proveedor, "Producto": e.oferta.nombre, "Marca": e.oferta.marca,
            "Presentación": e.presentacion.texto, "Precio": e.oferta.precio,
            f"Precio por {it.unidad_base}": round(e.precio_unitario) if e.precio_unitario else None,
            "Clasificación": ETIQUETA[e.clasificacion], "Elegible": e.elegible,
            "Disponibilidad": e.oferta.disponibilidad, "Costo": e.costo, "Observaciones": "; ".join(e.motivos),
            "Fuente": e.oferta.url,
        } for e in ev]), hide_index=True, width="stretch", column_config={
            "Precio": st.column_config.NumberColumn(format="$%d"),
            f"Precio por {it.unidad_base}": st.column_config.NumberColumn(format="$%d"),
            "Costo": st.column_config.NumberColumn(format="$%d"),
            "Fuente": st.column_config.LinkColumn("Fuente", display_text="ver"),
        })

with tab_sol:
    st.dataframe(pd.DataFrame([{
        "Producto": it.producto, "Cantidad": it.cantidad_texto,
        "Especificaciones": ", ".join(it.especificaciones) or "—",
        "Marca": f"{it.marca} ({'obligatoria' if it.marca_obligatoria else 'preferida'})" if it.marca else "—",
        "Término de búsqueda": it.termino_busqueda,
    } for it in est.solicitud.items]), hide_index=True, width="stretch")
    st.write(f"**Ciudad:** {est.solicitud.ciudad}")
    st.write("**Condiciones no verificadas:**")
    for c in est.condiciones_no_verificadas:
        st.write(f"- {c}")

with tab_cons:
    st.dataframe(pd.DataFrame([{
        "Proveedor": r.proveedor, "Término": r.termino, "Estado": r.estado, "Resultados": len(r.ofertas),
        "Fecha": r.fecha_consulta, "URL consultada": r.url_consulta, "Detalle": r.mensaje,
    } for r in est.resultados_consulta]), hide_index=True, width="stretch",
        column_config={"URL consultada": st.column_config.LinkColumn()})

with tab_rep:
    c1, c2 = st.columns(2)
    c1.download_button("Descargar reporte (.md)", est.reporte_md, file_name="presupuesto.md")
    c2.download_button("Descargar datos (.json)",
                       json.dumps(est.model_dump(mode="json", exclude={"reporte_md"}), ensure_ascii=False, indent=2),
                       file_name="presupuesto.json")
    st.markdown(est.reporte_md)

with tab_traza:
    for linea in est.historial:
        st.code(linea, language=None)
