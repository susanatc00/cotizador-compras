"""Armado de presupuestos y regla de recomendación (código determinista).

- Un presupuesto por proveedor: para cada artículo, la oferta elegible de menor
  costo total (equivalentes primero; sustitutos solo si se permiten y no hay
  equivalente). Se marca la cobertura.
- Una compra combinada: para cada artículo, la mejor oferta entre todos los
  proveedores. Solo es "viable" si cubre los artículos y usa 2 o más proveedores
  (si usa uno solo, coincide con el presupuesto de ese proveedor).
- Costos adicionales: los de cada proveedor usado. Un costo desconocido queda
  como `None` y el total final NO se confirma (nunca se asume envío = 0).
"""
from __future__ import annotations

import math

from cotizador.texto import cop
from cotizador.schemas import (
    CostoAdicional,
    ItemSolicitado,
    LineaPresupuesto,
    OfertaEvaluada,
    PresupuestoOpcion,
    Recomendacion,
)

# La compra combinada se recomienda solo si ahorra al menos este porcentaje frente
# a la mejor compra en un solo proveedor: cada proveedor extra es otro pedido y
# otro envío de costo desconocido.
AHORRO_MINIMO_COMBINADA = 0.05


def _mejor(evaluadas: list[OfertaEvaluada], permitir_sustitutos: bool) -> OfertaEvaluada | None:
    def clave(e: OfertaEvaluada):
        return (e.costo, e.precio_unitario or math.inf, e.excedente or 0)

    eq = [e for e in evaluadas if e.elegible and e.clasificacion == "equivalente"]
    if eq:
        return min(eq, key=clave)
    if permitir_sustitutos:
        su = [e for e in evaluadas if e.elegible and e.clasificacion == "sustituto"]
        if su:
            return min(su, key=clave)
    return None


def _cerrar_opcion(op: PresupuestoOpcion, costos_por_proveedor: dict[str, list[CostoAdicional]],
                   nombres: dict[str, str]) -> PresupuestoOpcion:
    cubiertas = [l for l in op.lineas if l.evaluada is not None]
    op.items_sin_cotizacion = [l.producto for l in op.lineas if l.evaluada is None]
    op.subtotal_productos = float(sum(l.evaluada.costo for l in cubiertas))
    if not cubiertas:
        op.cobertura = "nula"
    elif op.items_sin_cotizacion:
        op.cobertura = "parcial"
    elif any(l.evaluada.clasificacion == "sustituto" for l in cubiertas):
        op.cobertura = "completa_con_sustitutos"
    else:
        op.cobertura = "completa"

    usados = sorted({l.evaluada.oferta.proveedor_id for l in cubiertas})
    op.costos_adicionales = []
    for pid in usados:
        for c in costos_por_proveedor.get(pid, []):
            op.costos_adicionales.append(c.model_copy(update={"concepto": f"{c.concepto} ({nombres[pid]})"}))
    conocidos = sum(c.valor for c in op.costos_adicionales if c.valor is not None)
    op.total_conocido = op.subtotal_productos + conocidos

    op.pendientes = []
    for c in op.costos_adicionales:
        if c.valor is None:
            op.pendientes.append(f"{c.concepto}: costo desconocido")
    if op.items_sin_cotizacion:
        op.pendientes.append("productos sin cotización: " + ", ".join(op.items_sin_cotizacion))
    if op.cobertura == "completa_con_sustitutos":
        op.pendientes.append("incluye sustitutos que el usuario debe aceptar")
    op.total_final_confirmado = op.total_conocido if not op.pendientes else None
    return op


def armar_presupuestos(
    items: list[ItemSolicitado],
    evaluadas: list[OfertaEvaluada],
    proveedores: dict[str, str],  # id -> nombre
    costos_por_proveedor: dict[str, list[CostoAdicional]],
    permitir_sustitutos: bool = True,
) -> list[PresupuestoOpcion]:
    opciones: list[PresupuestoOpcion] = []

    for pid, nombre in proveedores.items():
        lineas = []
        for it in items:
            cand = [e for e in evaluadas if e.item_idx == it.idx and e.oferta.proveedor_id == pid]
            mejor = _mejor(cand, permitir_sustitutos)
            nota = "" if mejor else ("sin oferta elegible" if cand else "sin resultados")
            lineas.append(LineaPresupuesto(item_idx=it.idx, producto=it.producto, cantidad_texto=it.cantidad_texto,
                                           evaluada=mejor, nota=nota))
        op = PresupuestoOpcion(nombre=nombre, tipo="proveedor_unico", proveedores=[nombre], lineas=lineas,
                               cobertura="nula")
        opciones.append(_cerrar_opcion(op, costos_por_proveedor, proveedores))

    # --- Compra combinada ---
    lineas = []
    for it in items:
        cand = [e for e in evaluadas if e.item_idx == it.idx]
        mejor = _mejor(cand, permitir_sustitutos)
        lineas.append(LineaPresupuesto(item_idx=it.idx, producto=it.producto, cantidad_texto=it.cantidad_texto,
                                       evaluada=mejor, nota="" if mejor else "sin oferta elegible en ningún proveedor"))
    usados = sorted({l.evaluada.oferta.proveedor for l in lineas if l.evaluada})
    comb = PresupuestoOpcion(nombre="Compra combinada", tipo="combinada", proveedores=usados, lineas=lineas,
                             cobertura="nula")
    comb = _cerrar_opcion(comb, costos_por_proveedor, proveedores)
    if len(usados) < 2:
        comb.viable = False
        comb.nota = (f"No aplica: la mejor oferta de cada producto está en {usados[0]}, así que coincide con "
                     f"comprar todo allí." if usados else "No aplica: no hay ofertas elegibles.")
    else:
        comb.nota = f"Requiere {len(usados)} pedidos separados ({', '.join(usados)})."
    opciones.append(comb)
    return opciones


def recomendar(opciones: list[PresupuestoOpcion]) -> Recomendacion:
    """Regla de decisión explícita:
    1. Mayor cobertura (completa > completa con sustitutos > parcial).
    2. Dentro de la misma cobertura, menor total conocido.
    3. La combinada solo gana si ahorra >= AHORRO_MINIMO_COMBINADA frente al mejor
       proveedor único con la misma cobertura (cada pedido extra suma un envío
       de costo desconocido).
    """
    rango = {"completa": 0, "completa_con_sustitutos": 1, "parcial": 2, "nula": 3}
    viables = [o for o in opciones if o.viable and o.cobertura != "nula"]
    if not viables:
        return Recomendacion(opcion=None, criterio="Ninguna opción tiene productos cotizados.")
    mejor_rango = min(rango[o.cobertura] for o in viables)
    candidatas = [o for o in viables if rango[o.cobertura] == mejor_rango]
    if mejor_rango == 2:
        max_items = max(len(o.lineas) - len(o.items_sin_cotizacion) for o in candidatas)
        candidatas = [o for o in candidatas if len(o.lineas) - len(o.items_sin_cotizacion) == max_items]

    unicos = sorted([o for o in candidatas if o.tipo == "proveedor_unico"], key=lambda o: o.total_conocido)
    combinada = next((o for o in candidatas if o.tipo == "combinada"), None)
    cob = {0: "cobertura completa con productos equivalentes", 1: "cobertura completa usando sustitutos",
           2: "la mayor cobertura disponible (parcial)"}[mejor_rango]

    if unicos and combinada:
        ahorro = unicos[0].total_conocido - combinada.total_conocido
        pct = ahorro / unicos[0].total_conocido if unicos[0].total_conocido else 0
        if pct >= AHORRO_MINIMO_COMBINADA:
            return Recomendacion(
                opcion=combinada.nombre,
                criterio=(f"{cob}; la compra combinada ahorra {cop(ahorro)} ({pct:.1%}) en productos frente a "
                          f"{unicos[0].nombre}, por encima del umbral de {AHORRO_MINIMO_COMBINADA:.0%} que "
                          "compensa hacer más de un pedido."),
            )
        return Recomendacion(
            opcion=unicos[0].nombre,
            criterio=(f"{cob} y menor subtotal en un solo pedido; la compra combinada solo ahorraría "
                      f"{cop(ahorro)} ({pct:.1%}), menos del {AHORRO_MINIMO_COMBINADA:.0%} que justifica un "
                      "segundo pedido con envío de costo desconocido."),
        )
    elegida = unicos[0] if unicos else combinada
    return Recomendacion(opcion=elegida.nombre, criterio=f"{cob} y menor subtotal conocido.")
