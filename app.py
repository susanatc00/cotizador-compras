"""Interfaz web (Streamlit) del cotizador.

    uv run streamlit run app.py

Usa el mismo grafo que main.py. El grafo y su checkpointer se guardan en la
sesión para poder reanudar después de una aclaración (interrupt).
"""
from __future__ import annotations

import hmac
import json
import os
import time
import uuid

import streamlit as st

st.set_page_config(page_title="Cotizador de mercado", page_icon="🛒", layout="wide")

# Avance del arranque: visible en la página y en el registro (útil en Community Cloud).
_aviso = st.empty()
_t0 = time.monotonic()


def _paso(texto: str) -> None:
    linea = f"[app] {time.monotonic() - _t0:5.1f}s {texto}"
    print(linea, flush=True)
    _aviso.caption(f"Iniciando… {texto} (Streamlit {st.__version__})")


_paso("importando librerías")
import pandas as pd  # noqa: E402
from langgraph.types import Command  # noqa: E402


def _cargar_secrets() -> None:
    """En Streamlit Community Cloud la clave va en *Secrets*; se pasa al entorno
    para que cotizador.config la lea igual que desde .env."""
    try:
        for k, v in st.secrets.items():
            if isinstance(v, (str, int, float)):
                os.environ.setdefault(k, str(v))
    except Exception:  # noqa: BLE001 — en local no hay secrets.toml
        pass


_paso("leyendo secrets")
_cargar_secrets()

_paso("importando el cotizador")
from cotizador import reglas  # noqa: E402
from cotizador.config import hay_api_key  # noqa: E402
from cotizador.grafo import construir_grafo  # noqa: E402
from cotizador.proveedores import cargar_registro  # noqa: E402
from cotizador.schemas import EstadoCotizacion  # noqa: E402
from cotizador.texto import cop  # noqa: E402

_paso("revisando la clave de OpenAI")
_TIENE_KEY = hay_api_key()
_paso("listo")
_aviso.empty()

# Si se define CODIGO_ACCESO (en Secrets o .env), el LLM solo se activa con ese código:
# así la app puede ser pública sin que cualquiera gaste la clave de OpenAI.
CODIGO_ACCESO = os.getenv("CODIGO_ACCESO", "").strip()

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


def md(texto: str) -> str:
    """Escapa '$' para que Streamlit no interprete '$52.170 ... $55.850' como fórmula LaTeX."""
    return texto.replace("$", r"\$")


# --------------------------------------------------------------------------- estado de sesión
ss = st.session_state
ss.setdefault("texto", EJEMPLOS["Enunciado"])
ss.setdefault("grafo", None)
ss.setdefault("config", None)
ss.setdefault("preguntas", None)
ss.setdefault("estado", None)
ss.setdefault("pasos", [])


def construir(fuente: str, usar_llm: bool, sustitutos: bool, envio: float | None):
    registro = cargar_registro(fuente)
    kwargs = {}
    if not usar_llm:
        alias = [a for p in registro.proveedores.values() for a in [p.nombre, *p.alias]]
        alias += [a for info in registro.no_soportados.values() for a in info.get("alias", [])]
        kwargs = dict(interprete=reglas.interprete_reglas(alias), juez=reglas.juez_reglas(),
                      redactor=reglas.redactor_reglas())
    return construir_grafo(registro=registro, permitir_sustitutos=sustitutos, dir_salida=None,
                           envio_estimado=envio, usar_envio_de_config=False, **kwargs)


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
    tiene_key = _TIENE_KEY
    autorizado = True
    if tiene_key and CODIGO_ACCESO:
        codigo = st.text_input("Código de acceso para usar el LLM", type="password",
                               help="Sin el código, la app funciona con reglas (sin OpenAI).")
        autorizado = hmac.compare_digest(codigo.strip(), CODIGO_ACCESO)
        if codigo and not autorizado:
            st.error("Código incorrecto.")
    puede_llm = tiene_key and autorizado
    usar_llm = st.toggle("Usar LLM (OpenAI)", value=puede_llm, disabled=not puede_llm,
                         help="Sin OPENAI_API_KEY (o sin el código de acceso) se usan las reglas de respaldo.")
    fuente = st.radio("Fuente de precios", ["auto", "web", "catalogo"], index=0,
                      format_func={"auto": "Web (con respaldo en catálogo)", "web": "Solo web en vivo",
                                   "catalogo": "Catálogo de referencia (sin internet)"}.get)
    sustitutos = st.checkbox("Permitir sustitutos en el presupuesto", value=True)
    estimar_envio = st.checkbox("Estimar el domicilio", value=True,
                                help="Las tiendas solo calculan el domicilio en el checkout. El estimado se usa "
                                     "para comparar opciones, pero el total final sigue sin confirmar.")
    envio = st.number_input("Domicilio estimado por pedido (COP)", min_value=0, step=500,
                            value=int(cargar_registro("catalogo").envio_estimado[0] or 5000),
                            disabled=not estimar_envio) if estimar_envio else None
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
    ss.grafo = construir(fuente, usar_llm, sustitutos, float(envio) if envio is not None else None)
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
    st.success(f"### Recomendación: {rec.opcion}\n{md(rec.criterio)}")
    if rec.texto:
        st.write(md(rec.texto))

viables = [o for o in est.opciones if o.viable]
cols = st.columns(len(viables) or 1)
for col, op in zip(cols, viables):
    titulo = op.nombre if op.tipo == "combinada" else f"Todo en {op.nombre}"
    final = cop(op.total_final_confirmado) if op.total_final_confirmado is not None else "No confirmado"
    if op.total_estimado is not None:
        col.metric(titulo, cop(op.total_estimado), help="Total estimado: productos + domicilio estimado")
        n = len(op.proveedores)
        col.caption(f"Productos: **{cop(op.subtotal_productos)}** + {n} domicilio{'s' if n > 1 else ''} "
                    f"estimado{'s' if n > 1 else ''}  \nCobertura: **{COBERTURA[op.cobertura]}**  \n"
                    f"Total final: **{final}**")
    else:
        col.metric(titulo, cop(op.subtotal_productos), help="Subtotal de productos")
        col.caption(f"Cobertura: **{COBERTURA[op.cobertura]}**  \nTotal final: **{final}**")
if rec and rec.punto_equilibrio_domicilio is not None:
    st.caption(md("Punto de equilibrio: la compra combinada conviene solo si cada domicilio adicional cuesta menos "
                  f"de {cop(rec.punto_equilibrio_domicilio)}."))

if est.suposiciones:
    with st.expander(f"Suposiciones explícitas ({len(est.suposiciones)})"):
        for s in est.suposiciones:
            st.write(md(f"- {s}"))

tab_pres, tab_ofertas, tab_sol, tab_cons, tab_rep, tab_traza = st.tabs(
    ["Presupuestos", "Ofertas encontradas", "Solicitud interpretada", "Consultas y fuentes", "Reporte completo",
     "Traza del grafo"])

with tab_pres:
    for op in est.opciones:
        st.subheader(f"Compra completa en {op.nombre}" if op.tipo == "proveedor_unico" else op.nombre)
        if not op.viable:
            st.info(md(op.nota))
            continue
        if op.nota:
            st.caption(md(op.nota))
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
        costos += [{"Concepto": c.concepto,
                    "Valor": cop(c.valor) if c.valor is not None else
                    (f"estimado {cop(c.estimado)} (no confirmado)" if c.estimado is not None else "desconocido")}
                   for c in op.costos_adicionales]
        costos += [{"Concepto": "Total con costos conocidos", "Valor": cop(op.total_conocido)}]
        if op.total_estimado is not None:
            costos += [{"Concepto": "Total estimado (con domicilio estimado)", "Valor": cop(op.total_estimado)}]
        costos += [
                   {"Concepto": "Total final confirmado",
                    "Valor": cop(op.total_final_confirmado) if op.total_final_confirmado is not None else "No confirmado"}]
        st.table(pd.DataFrame(costos).set_index("Concepto"))
        if op.pendientes:
            st.caption(md("Pendiente: " + "; ".join(op.pendientes)))

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
        st.write(md(f"- {c}"))

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
    st.markdown(md(est.reporte_md))

with tab_traza:
    for linea in est.historial:
        st.code(linea, language=None)
