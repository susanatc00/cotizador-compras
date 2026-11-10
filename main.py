"""Cotizador de compras de mercado con LangGraph + LangChain.

Uso:
    uv run python main.py --texto "Necesito 5 kg de arroz, 2 litros de aceite y 3 kg de azúcar. \
Consulta dos supermercados y prepara un presupuesto para Medellín."
    uv run python main.py                         # pregunta la solicitud por consola
    uv run python main.py --no-interactivo ...    # no pregunta: registra suposiciones
    uv run python main.py --fuente catalogo ...   # usa el catálogo de referencia (sin internet)
    uv run python main.py --sin-llm ...           # reglas en lugar de OpenAI (sin API key)
    uv run python main.py --mermaid               # imprime el grafo
    uv run python main.py --capturar-catalogo arroz aceite azucar   # actualiza el CSV de referencia
"""
from __future__ import annotations

import argparse
import csv
import sys
import uuid
from datetime import datetime
from pathlib import Path

from langgraph.types import Command

from cotizador.config import hay_api_key
from cotizador.grafo import construir_grafo
from cotizador.proveedores import RAIZ, cargar_registro
from cotizador.schemas import EstadoCotizacion

CATALOGO_REFERENCIA = RAIZ / "data" / "catalogos" / "referencia_mercado.csv"
COLUMNAS = ["proveedor_id", "proveedor", "termino", "nombre", "marca", "precio", "moneda", "url", "sku",
            "disponibilidad", "fecha_consulta", "fuente_consulta"]


def _consola_utf8() -> None:
    for flujo in (sys.stdout, sys.stderr):
        try:
            flujo.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass


def capturar_catalogo(terminos: list[str], destino: Path) -> None:
    """Consulta la web una vez y guarda un catálogo de referencia con URL y fecha."""
    registro = cargar_registro("web")
    filas = []
    for prov in registro.proveedores.values():
        for i, t in enumerate(terminos):
            r = prov.buscar(t, i)
            print(f"  {prov.nombre:10} {t:12} {r.estado:16} {len(r.ofertas)} productos")
            for o in r.ofertas:
                filas.append({"proveedor_id": o.proveedor_id, "proveedor": o.proveedor, "termino": t,
                              "nombre": o.nombre, "marca": o.marca or "", "precio": o.precio or "",
                              "moneda": o.moneda or "", "url": o.url or "", "sku": o.sku or "",
                              "disponibilidad": o.disponibilidad, "fecha_consulta": o.fecha_consulta,
                              "fuente_consulta": o.fuente_consulta})
    destino.parent.mkdir(parents=True, exist_ok=True)
    with destino.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNAS)
        w.writeheader()
        w.writerows(filas)
    print(f"Catálogo de referencia guardado: {destino} ({len(filas)} filas)")


def construir(args):
    usar_llm = not args.sin_llm
    if usar_llm and not hay_api_key():
        print("Aviso: no hay OPENAI_API_KEY en .env; se usan las reglas de respaldo (--sin-llm).\n")
        usar_llm = False
    registro = cargar_registro(args.fuente)
    kwargs = {}
    if not usar_llm:
        from cotizador import reglas

        alias = [a for p in registro.proveedores.values() for a in [p.nombre, *p.alias]]
        alias += [a for info in registro.no_soportados.values() for a in info.get("alias", [])]
        kwargs = dict(interprete=reglas.interprete_reglas(alias), juez=reglas.juez_reglas(),
                      redactor=reglas.redactor_reglas())
    return construir_grafo(registro=registro, permitir_sustitutos=not args.sin_sustitutos,
                           dir_salida=Path(args.salida), envio_estimado=args.envio_estimado,
                           usar_envio_de_config=not args.sin_envio_estimado, **kwargs)


def main() -> None:
    _consola_utf8()
    p = argparse.ArgumentParser(description="Cotizador de compras de mercado (LangGraph)")
    p.add_argument("--texto", default="", help="Solicitud en lenguaje natural.")
    p.add_argument("--no-interactivo", action="store_true",
                   help="No pedir aclaraciones: registrar suposiciones explícitas.")
    p.add_argument("--fuente", choices=["auto", "web", "catalogo"], default="auto",
                   help="auto = web con respaldo en catálogo (por defecto); web = solo en vivo; "
                        "catalogo = solo el CSV de referencia.")
    p.add_argument("--sin-llm", action="store_true", help="Usar reglas en lugar de OpenAI.")
    p.add_argument("--sin-sustitutos", action="store_true", help="No incluir sustitutos en los presupuestos.")
    p.add_argument("--envio-estimado", type=float, default=None, metavar="COP",
                   help="Domicilio estimado por pedido (por defecto el de config/proveedores.json: 5000). "
                        "Solo alimenta el total estimado; el total final sigue sin confirmar.")
    p.add_argument("--sin-envio-estimado", action="store_true",
                   help="No estimar el domicilio: los envíos quedan como desconocidos.")
    p.add_argument("--salida", default=str(RAIZ / "salidas"), help="Carpeta para el reporte .md y .json.")
    p.add_argument("--mermaid", action="store_true", help="Imprime el diagrama Mermaid del grafo y termina.")
    p.add_argument("--capturar-catalogo", nargs="+", metavar="TERMINO",
                   help="Consulta la web y reescribe data/catalogos/referencia_mercado.csv.")
    args = p.parse_args()

    if args.capturar_catalogo:
        capturar_catalogo(args.capturar_catalogo, CATALOGO_REFERENCIA)
        return

    if args.mermaid:
        from langchain_core.runnables import RunnableLambda

        nada = RunnableLambda(lambda _: None)
        print(construir_grafo(interprete=nada, juez=nada, redactor=nada).get_graph().draw_mermaid())
        return

    texto = args.texto.strip()
    if not texto:
        texto = input("¿Qué necesita comprar? > ").strip()
    grafo = construir(args)
    config = {"configurable": {"thread_id": str(uuid.uuid4())}}
    entrada = EstadoCotizacion(texto_usuario=texto, modo_interactivo=not args.no_interactivo)

    inicio = datetime.now()
    resultado = grafo.invoke(entrada, config)
    while "__interrupt__" in resultado:
        preguntas = resultado["__interrupt__"][0].value["preguntas"]
        print("\nNecesito aclarar algunos datos:")
        for q in preguntas:
            print(f"  - {q}")
        respuesta = input("Su respuesta (Enter para continuar con suposiciones) > ")
        resultado = grafo.invoke(Command(resume=respuesta), config)

    estado = EstadoCotizacion.model_validate(resultado)
    print("\n" + estado.reporte_md)
    print("--- Traza de nodos ---")
    for linea in estado.historial:
        print(linea)
    print(f"\nTiempo total: {(datetime.now() - inicio).total_seconds():.1f} s")
    for a in estado.archivos_salida:
        print(f"Archivo generado: {a}")


if __name__ == "__main__":
    main()
