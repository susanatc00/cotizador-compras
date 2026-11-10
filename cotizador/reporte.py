"""Reporte final en Markdown (y JSON con todo el estado para trazabilidad)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from cotizador.texto import cop
from cotizador.unidades import UNIDAD_BASE

if TYPE_CHECKING:
    from cotizador.schemas import EstadoCotizacion, OfertaEvaluada, PresupuestoOpcion

# Ofertas que se muestran por proveedor y clasificación (el JSON trae todas).
MAX_POR_CLASE = {"equivalente": 5, "sustituto": 3, "no_comparable": 2}

_ETIQUETA = {"equivalente": "Equivalente", "sustituto": "Sustituto", "no_comparable": "No comparable",
             "descartado": "Descartado"}
_MECANISMO = {"jsonld_publico": "JSON-LD público (consulta en vivo)",
              "catalogo_referencia": "Catálogo de referencia (CSV)"}
_COBERTURA = {"completa": "Completa", "completa_con_sustitutos": "Completa con sustitutos",
              "parcial": "Parcial", "nula": "Sin cobertura"}


def _celda(texto: str | None) -> str:
    return (texto or "").replace("|", "/").replace("\n", " ")


def _link(texto: str, url: str | None) -> str:
    return f"[{_celda(texto)}]({url})" if url else _celda(texto)


def _precio_unitario(e: "OfertaEvaluada", unidad: str) -> str:
    return f"{cop(e.precio_unitario)}/{unidad}" if e.precio_unitario is not None else "—"


def _tabla_opcion(op: "PresupuestoOpcion", unidades: dict[int, str]) -> list[str]:
    multi = op.tipo == "combinada"
    cab = "| Producto | Cantidad pedida | Oferta elegida |" + (" Proveedor |" if multi else "") + \
          " Tipo | Precio empaque | Empaques | Costo | Excedente |"
    filas = [cab, "|" + "---|" * (cab.count("|") - 1)]
    for l in op.lineas:
        e = l.evaluada
        if e is None:
            filas.append(f"| {l.producto} | {l.cantidad_texto} | **Sin cotización** ({l.nota}) |" +
                         (" — |" if multi else "") + " — | — | — | — | — |")
            continue
        exc = f"{e.excedente:g} {unidades[l.item_idx]}" if e.excedente else "0"
        filas.append(
            f"| {l.producto} | {l.cantidad_texto} | {_link(e.oferta.nombre, e.oferta.url)} |" +
            (f" {e.oferta.proveedor} |" if multi else "") +
            f" {_ETIQUETA[e.clasificacion]} | {cop(e.oferta.precio)} | {e.empaques} | {cop(e.costo)} | {exc} |"
        )
    return filas


def generar_reporte(state: "EstadoCotizacion") -> str:
    sol = state.solicitud
    out: list[str] = []
    ahora = datetime.now().strftime("%Y-%m-%d %H:%M")
    out += [f"# Presupuesto de compra — {sol.ciudad}", "",
            f"_Generado el {ahora}. Precios en pesos colombianos (COP) tal como los publica cada fuente._", "",
            "## 1. Solicitud interpretada", "", f"> {state.texto_usuario.strip()}".replace("\n", "\n> "), "",
            "| # | Producto | Cantidad | Especificaciones | Marca | Presentación |", "|---|---|---|---|---|---|"]
    for it in sol.items:
        marca = f"{it.marca} ({'obligatoria' if it.marca_obligatoria else 'preferida'})" if it.marca else "—"
        pres = (f"{it.presentacion_base:g} {it.unidad_base} ({'obligatoria' if it.presentacion_obligatoria else 'sugerida'})"
                if it.presentacion_base else "—")
        out.append(f"| {it.idx + 1} | {it.producto} | {it.cantidad_texto} | {', '.join(it.especificaciones) or '—'} | "
                   f"{marca} | {pres} |")
    out += ["", f"**Ciudad:** {sol.ciudad}  ", "**Proveedores consultados:** " +
            ", ".join(sorted({r.proveedor for r in state.resultados_consulta})), ""]
    if state.suposiciones:
        out += ["**Suposiciones explícitas:**", ""] + [f"- {s}" for s in state.suposiciones] + [""]

    # --- Consultas realizadas ---
    out += ["## 2. Consultas realizadas", "",
            "| Proveedor | Término | Mecanismo | Estado | Resultados | Fecha de consulta | Fuente |",
            "|---|---|---|---|---|---|---|"]
    for r in sorted(state.resultados_consulta, key=lambda r: (r.proveedor, r.item_idx)):
        mec = _MECANISMO.get(r.ofertas[0].mecanismo, r.ofertas[0].mecanismo) if r.ofertas else "—"
        fuente = _link("página consultada", r.url_consulta) if (r.url_consulta or "").startswith("http") \
            else _celda(r.url_consulta)
        out.append(f"| {r.proveedor} | {r.termino} | {mec} | {r.estado} | {len(r.ofertas)} | "
                   f"{r.fecha_consulta} | {fuente} |")
    out.append("")

    # --- Ofertas por producto ---
    unidades = {it.idx: it.unidad_base for it in sol.items}
    out += ["## 3. Ofertas encontradas por producto", "",
            "Clasificación: **Equivalente** = mismo producto y especificación; **Sustituto** = mismo uso, otra "
            "variedad o calidad; **No comparable** = falta precio o contenido, o la unidad no se puede convertir. "
            "Los productos descartados (no corresponden a lo pedido) se cuentan pero no se listan.", ""]
    for it in sol.items:
        ev = [e for e in state.evaluadas if e.item_idx == it.idx]
        descartadas = sum(1 for e in ev if e.clasificacion == "descartado")
        out += [f"### {it.idx + 1}. {it.producto} — {it.cantidad_texto}", ""]
        visibles = [e for e in ev if e.clasificacion != "descartado"]
        if not visibles:
            out += ["_Ninguna oferta corresponde a este producto._", ""]
            continue
        out += ["| Proveedor | Producto | Marca | Presentación | Precio | Precio por unidad | Clasificación | "
                "Disponibilidad | Empaques → costo | Observaciones |", "|---|---|---|---|---|---|---|---|---|---|"]
        for prov in sorted({e.oferta.proveedor for e in visibles}):
            delprov = [e for e in visibles if e.oferta.proveedor == prov]
            mostrar = []
            for clase, n in MAX_POR_CLASE.items():
                mostrar += [e for e in delprov if e.clasificacion == clase][:n]
            for e in mostrar:
                costo = f"{e.empaques} → {cop(e.costo)}" if e.costo is not None else "—"
                clasif = _ETIQUETA[e.clasificacion] + ("" if e.elegible else " (no elegible)")
                out.append(
                    f"| {prov} | {_link(e.oferta.nombre, e.oferta.url)} | {_celda(e.oferta.marca) or '—'} | "
                    f"{_celda(e.presentacion.texto)} | {cop(e.oferta.precio)} | {_precio_unitario(e, it.unidad_base)} | "
                    f"{clasif} | {e.oferta.disponibilidad} | {costo} | {_celda('; '.join(e.motivos))} |"
                )
            if len(delprov) > len(mostrar):
                out.append(f"| {prov} | _… y {len(delprov) - len(mostrar)} ofertas más (ver JSON)_ "
                           "| | | | | | | | |")
        out += ["", f"_{descartadas} resultados descartados por no corresponder al producto._", ""]

    # --- Presupuestos ---
    out += ["## 4. Presupuestos", ""]
    for op in state.opciones:
        titulo = f"Compra completa en {op.nombre}" if op.tipo == "proveedor_unico" else op.nombre
        out += [f"### {titulo}", ""]
        if not op.viable:
            out += [f"_{op.nota}_", ""]
            continue
        if op.nota:
            out += [f"_{op.nota}_", ""]
        out += _tabla_opcion(op, unidades) + [""]
        out += ["| Concepto | Valor |", "|---|---|", f"| Subtotal de productos | **{cop(op.subtotal_productos)}** |"]
        for c in op.costos_adicionales:
            out.append(f"| {c.concepto} | {cop(c.valor)}{' — ' + _celda(c.nota) if c.valor is None else ''} |")
        out.append(f"| Total con costos conocidos | {cop(op.total_conocido)} |")
        tf = cop(op.total_final_confirmado) if op.total_final_confirmado is not None else "**No confirmado**"
        out += [f"| Total final confirmado | {tf} |", "", f"Cobertura: **{_COBERTURA[op.cobertura]}**."]
        if op.pendientes:
            out += ["", "Pendiente para confirmar el total: " + "; ".join(op.pendientes) + "."]
        out.append("")

    # --- Sin cotización y condiciones ---
    comb = next((o for o in state.opciones if o.tipo == "combinada"), None)
    sin = comb.items_sin_cotizacion if comb else []
    out += ["## 5. Productos sin cotización y condiciones no verificadas", ""]
    out += [f"- **Sin cotización en ningún proveedor:** {', '.join(sin)}" if sin else
            "- Todos los productos tienen al menos una oferta elegible."]
    for op in state.opciones:
        if op.tipo == "proveedor_unico" and op.items_sin_cotizacion:
            out.append(f"- Sin cotización en {op.nombre}: {', '.join(op.items_sin_cotizacion)}")
    out += [f"- {c}" for c in state.condiciones_no_verificadas]
    out += ["- Los costos de envío desconocidos **no** se suman como cero: por eso el total final queda sin confirmar.",
            "- El contenido de cada empaque se leyó del nombre del producto; las presentaciones marcadas como "
            "\"inferidas\" deben confirmarse en la página.", ""]

    # --- Recomendación ---
    rec = state.recomendacion
    out += ["## 6. Recomendación", ""]
    if rec and rec.opcion:
        out += [f"**Opción recomendada: {rec.opcion}.**", "", f"Criterio aplicado: {rec.criterio}", ""]
    if rec and rec.texto:
        out += [rec.texto, ""]

    # --- Fuentes ---
    out += ["## 7. Fuentes", ""]
    usadas = {}
    for op in state.opciones:
        for l in op.lineas:
            if l.evaluada and l.evaluada.oferta.url:
                o = l.evaluada.oferta
                usadas[o.url] = f"{o.proveedor} — {o.nombre} (consultado {o.fecha_consulta})"
    out += [f"- [{_celda(t)}]({u})" for u, t in usadas.items()]
    consultas = sorted({(r.proveedor, r.url_consulta) for r in state.resultados_consulta if r.url_consulta})
    out += ["", "Páginas de búsqueda consultadas:", ""] + [
        f"- {p}: {u if not u.startswith('http') else f'<{u}>'}" for p, u in consultas]
    out.append("")
    return "\n".join(out)


def guardar_salidas(state: "EstadoCotizacion", md: str, carpeta: Path) -> list[str]:
    carpeta.mkdir(parents=True, exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d_%H%M%S")
    ruta_md = carpeta / f"presupuesto_{marca}.md"
    ruta_json = carpeta / f"presupuesto_{marca}.json"
    ruta_md.write_text(md, encoding="utf-8")
    datos = state.model_dump(mode="json", exclude={"reporte_md"})
    ruta_json.write_text(json.dumps(datos, ensure_ascii=False, indent=2), encoding="utf-8")
    return [str(ruta_md), str(ruta_json)]


__all__ = ["generar_reporte", "guardar_salidas", "cop", "UNIDAD_BASE"]
