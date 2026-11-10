"""Grafo completo con LLM falso y catálogos locales (sin red ni API)."""
import pytest
from langchain_core.runnables import RunnableLambda
from langgraph.types import Command

from cotizador import reglas
from cotizador.grafo import construir_grafo
from cotizador.schemas import EstadoCotizacion
from tests.conftest import crear_registro, interprete_fijo, item, solicitud

CONFIG = {"configurable": {"thread_id": "prueba"}}


def _grafo(interprete, registro=None, juez=None, redactor=None, **kw):
    return construir_grafo(interprete=interprete, juez=juez or reglas.juez_reglas(),
                           redactor=redactor or reglas.redactor_reglas(),
                           registro=registro or crear_registro(), **kw)


def _estado(resultado) -> EstadoCotizacion:
    return EstadoCotizacion.model_validate({k: v for k, v in resultado.items() if not k.startswith("__")})


def test_flujo_completo_dos_proveedores():
    interp = interprete_fijo(solicitud([item("arroz", "arroz", 5, "kg"), item("aceite", "aceite", 2, "L")],
                                       cantidad=2))
    est = _estado(_grafo(interp).invoke(EstadoCotizacion(texto_usuario="x"), CONFIG))

    # Fan-out con Send: un resultado por proveedor y artículo.
    assert {(r.proveedor_id, r.item_idx) for r in est.resultados_consulta} == {
        ("tienda_a", 0), ("tienda_a", 1), ("tienda_b", 0), ("tienda_b", 1)}
    clasif = {e.oferta.sku: e.clasificacion for e in est.evaluadas}
    assert clasif["A3"] == "sustituto"  # arroz integral
    assert clasif["A4"] == "descartado"  # arroz con pollo
    assert clasif["A6"] == "no_comparable"  # sin contenido legible
    assert clasif["B5"] == "no_comparable"  # sin precio

    a, b, comb = est.opciones
    assert a.cobertura == b.cobertura == "completa"
    # Tienda A: arroz 5 x 1 kg (21.000 < bolsa de 5 kg a 21.950) + aceite 3 x 900 mL (47.700)
    # Tienda B: arroz 5 kg 24.430 (el de 10 kg está agotado) + aceite 3 L 22.890
    assert a.lineas[0].evaluada.oferta.sku == "A2" and a.lineas[0].evaluada.empaques == 5
    assert a.subtotal_productos == 5 * 4200 + 3 * 15900
    assert b.subtotal_productos == 24430 + 22890
    assert all(o.total_final_confirmado is None for o in est.opciones)  # envío desconocido
    assert comb.viable and comb.subtotal_productos == 5 * 4200 + 22890
    assert est.recomendacion.opcion == "Compra combinada" and est.recomendacion.texto
    assert "## 4. Presupuestos" in est.reporte_md and "No confirmado" in est.reporte_md
    assert est.historial[0].startswith("[interpretar_solicitud]")
    assert est.historial[-1].startswith("[generar_reporte]")


def test_aclaracion_con_interrupt_y_reanudacion():
    incompleta = solicitud([item("arroz", "arroz", None, None)], ciudad=None)
    completa = solicitud([item("arroz", "arroz", 5, "kg")], ciudad="Envigado")
    interp = interprete_fijo(incompleta, completa)
    g = _grafo(interp)

    r = g.invoke(EstadoCotizacion(texto_usuario="necesito arroz", modo_interactivo=True), CONFIG)
    assert "__interrupt__" in r
    preguntas = r["__interrupt__"][0].value["preguntas"]
    assert any("cantidad de arroz" in p for p in preguntas) and any("ciudad" in p for p in preguntas)

    est = _estado(g.invoke(Command(resume="5 kg, en Envigado"), CONFIG))
    assert "Aclaración del usuario: 5 kg, en Envigado" in interp.llamadas[1]
    assert est.solicitud.ciudad == "Envigado" and est.solicitud.items[0].cantidad == 5
    assert est.reporte_md


def test_sin_interaccion_registra_suposiciones():
    interp = interprete_fijo(solicitud([item("arroz", "arroz", None, None)], ciudad=None))
    est = _estado(_grafo(interp).invoke(EstadoCotizacion(texto_usuario="arroz", modo_interactivo=False), CONFIG))
    assert any("se cotiza 1 empaque" in s for s in est.suposiciones)
    assert any("se asume Medellín" in s for s in est.suposiciones)
    assert est.solicitud.items[0].dimension == "unidad"


def test_proveedor_no_soportado_queda_explicado():
    interp = interprete_fijo(solicitud([item("arroz", "arroz", 1, "kg")], mencionados=["Éxito"]))
    est = _estado(_grafo(interp).invoke(EstadoCotizacion(texto_usuario="x"), CONFIG))
    assert any("Éxito" in s for s in est.suposiciones)
    assert len(est.solicitud.proveedores_ids) == 2


def test_juez_que_falla_usa_reglas():
    def falla(_):
        raise TimeoutError("sin red")

    interp = interprete_fijo(solicitud([item("arroz", "arroz", 5, "kg")]))
    est = _estado(_grafo(interp, juez=RunnableLambda(falla)).invoke(EstadoCotizacion(texto_usuario="x"), CONFIG))
    assert any("juez LLM falló" in h for h in est.historial)
    assert any(e.clasificacion == "equivalente" for e in est.evaluadas)


def test_producto_sin_resultados_aparece_sin_cotizacion():
    interp = interprete_fijo(solicitud([item("arroz", "arroz", 1, "kg"), item("detergente", "detergente", 1, "L")]))
    est = _estado(_grafo(interp).invoke(EstadoCotizacion(texto_usuario="x"), CONFIG))
    assert est.opciones[-1].items_sin_cotizacion == ["detergente"]
    assert "Sin cotización en ningún proveedor:** detergente" in est.reporte_md


def test_restriccion_de_marca_obligatoria():
    interp = interprete_fijo(solicitud([item("arroz", "arroz", 5, "kg", marca="Roa", marca_obligatoria=True)]))
    est = _estado(_grafo(interp).invoke(EstadoCotizacion(texto_usuario="x"), CONFIG))
    a, b, _ = est.opciones
    assert a.items_sin_cotizacion == ["arroz"]  # Tienda A solo tiene Diana
    assert b.lineas[0].evaluada.oferta.sku == "B1"


@pytest.mark.parametrize("texto,n_items,ciudad", [
    ("Necesito 5 kg de arroz, 2 litros de aceite y 3 kg de azúcar. Consulta dos supermercados y prepara "
     "un presupuesto para Medellín.", 3, "Medellín"),
])
def test_interprete_por_reglas(texto, n_items, ciudad):
    s = reglas.interpretar_por_reglas(texto)
    assert len(s.items) == n_items and s.ciudad == ciudad and s.cantidad_proveedores == 2
    assert [(i.termino_busqueda, i.cantidad, i.unidad) for i in s.items] == [
        ("arroz", 5, "kg"), ("aceite", 2, "L"), ("azucar", 3, "kg")]


def test_envio_estimado_en_el_grafo():
    interp = interprete_fijo(solicitud([item("arroz", "arroz", 5, "kg"), item("aceite", "aceite", 2, "L")]))
    est = _estado(_grafo(interp, envio_estimado=5000).invoke(EstadoCotizacion(texto_usuario="x"), CONFIG))
    a, b, comb = est.opciones
    # B = 47.320 + 5.000 = 52.320; combinada = 43.890 + 10.000 = 53.890 -> gana Tienda B
    assert b.total_estimado == 47320 + 5000 and comb.total_estimado == 43890 + 10000
    assert est.recomendacion.opcion == "Tienda B"
    assert est.recomendacion.punto_equilibrio_domicilio == 47320 - 43890
    assert all(o.total_final_confirmado is None for o in est.opciones)
    assert "Total estimado (con domicilio estimado)" in est.reporte_md
