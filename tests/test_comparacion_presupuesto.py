"""Normalización, clasificación y presupuesto (sin LLM)."""
import pytest

from cotizador.comparacion import evaluar_oferta
from cotizador.presupuesto import armar_presupuestos, recomendar
from cotizador.schemas import CostoAdicional, ItemSolicitado, Oferta, VeredictoOferta


def _item(idx=0, producto="arroz", cantidad=5.0, dimension="masa", unidad="kg", **kw):
    return ItemSolicitado(idx=idx, texto_original="", producto=producto, termino_busqueda=producto,
                          cantidad=cantidad, dimension=dimension, unidad_base=unidad,
                          cantidad_texto=f"{cantidad:g} {unidad}", **kw)


def _oferta(nombre, precio, prov="a", marca=None, disp="disponible", moneda="COP", sku=None):
    return Oferta(id=f"{prov}:{sku or nombre}", proveedor_id=prov, proveedor=prov.upper(), nombre=nombre,
                  marca=marca, presentacion_texto=nombre, precio=precio, moneda=moneda, url=f"https://{prov}/{sku}",
                  fecha_consulta="2026-11-09T10:00:00-05:00", disponibilidad=disp, mecanismo="catalogo_referencia",
                  fuente_consulta="prueba")


def _v(o, cat="equivalente"):
    return VeredictoOferta(id=o.id, categoria=cat, motivo="prueba")


def test_empaques_costo_y_precio_unitario():
    o = _oferta("Arroz Diana 1 Kg", 4200)
    e = evaluar_oferta(_item(cantidad=2.5), o, _v(o))
    assert e.clasificacion == "equivalente" and e.elegible
    assert e.precio_unitario == 4200 and e.empaques == 3 and e.costo == 12600
    assert e.excedente == pytest.approx(0.5)


def test_no_comparable_sin_precio_o_unidad_incompatible():
    o = _oferta("Aceite Soya x1000ml", None)
    assert evaluar_oferta(_item(producto="aceite", dimension="volumen", unidad="L"), o, _v(o)).clasificacion == "no_comparable"
    o = _oferta("Endulzante Stevia liquido x250ml", 9000)
    e = evaluar_oferta(_item(producto="azucar"), o, _v(o, "sustituto"))
    assert e.clasificacion == "no_comparable" and not e.elegible and "se pidió en kg" in e.motivos[-1]
    o = _oferta("Aceite Mezcla Vegetal Sin Peso", 9000)
    assert evaluar_oferta(_item(producto="aceite", dimension="volumen", unidad="L"), o, _v(o)).clasificacion == "no_comparable"
    o = _oferta("Arroz 1 kg", 2, moneda="USD")
    assert evaluar_oferta(_item(), o, _v(o)).clasificacion == "no_comparable"


def test_no_corresponde_se_descarta():
    o = _oferta("Arroz con Pollo 250 g", 8900)
    e = evaluar_oferta(_item(), o, _v(o, "no_corresponde"))
    assert e.clasificacion == "descartado" and not e.elegible


def test_restricciones_de_marca_y_presentacion():
    roa = _oferta("Arroz Roa x5kg", 24430, marca="ROA")
    diana = _oferta("Arroz Diana x1kg", 4690, marca="DIANA")
    exige = _item(marca="Diana", marca_obligatoria=True)
    assert not evaluar_oferta(exige, roa, _v(roa)).elegible
    assert evaluar_oferta(exige, diana, _v(diana)).elegible
    prefiere = _item(marca="Diana", marca_obligatoria=False)
    e = evaluar_oferta(prefiere, roa, _v(roa))
    assert e.elegible and any("preferida" in m for m in e.motivos)

    bolsas_1kg = _item(presentacion_base=1.0, presentacion_obligatoria=True)
    assert not evaluar_oferta(bolsas_1kg, roa, _v(roa)).elegible
    assert evaluar_oferta(bolsas_1kg, diana, _v(diana)).elegible
    paca = _oferta("Arroz Diana x25und x1000g c-u", 99000)
    assert evaluar_oferta(bolsas_1kg, paca, _v(paca)).elegible  # 1 kg por unidad


def test_agotado_no_es_elegible():
    o = _oferta("Arroz Diana x10kg", 44290, disp="agotado")
    e = evaluar_oferta(_item(), o, _v(o))
    assert e.clasificacion == "equivalente" and not e.elegible


def _evaluar_todo(items, pares):
    return [evaluar_oferta(it, o, _v(o, cat)) for it, o, cat in pares]


def test_envio_desconocido_no_confirma_total():
    it = _item()
    a = _oferta("Arroz 5 kg", 20000, prov="a", sku="1")
    b = _oferta("Arroz 5 kg", 21000, prov="b", sku="2")
    ev = _evaluar_todo([it], [(it, a, "equivalente"), (it, b, "equivalente")])
    costos = {"a": [CostoAdicional(concepto="Envío", valor=None)], "b": [CostoAdicional(concepto="Envío", valor=5000)]}
    ops = armar_presupuestos([it], ev, {"a": "A", "b": "B"}, costos)
    op_a, op_b, comb = ops
    assert op_a.subtotal_productos == 20000 and op_a.total_final_confirmado is None
    assert any("desconocido" in p for p in op_a.pendientes)
    assert op_b.total_conocido == 26000 and op_b.total_final_confirmado == 26000
    assert not comb.viable and "coincide" in comb.nota


def test_combinada_y_umbral_de_recomendacion():
    arroz, aceite = _item(0), _item(1, "aceite", 2.0, "volumen", "L")
    pares = [
        (arroz, _oferta("Arroz 5 kg", 20000, "a", sku="1"), "equivalente"),
        (arroz, _oferta("Arroz 5 kg", 30000, "b", sku="2"), "equivalente"),
        (aceite, _oferta("Aceite 1 L", 20000, "a", sku="3"), "equivalente"),
        (aceite, _oferta("Aceite 1 L", 9000, "b", sku="4"), "equivalente"),
    ]
    ev = _evaluar_todo([arroz, aceite], pares)
    costos = {"a": [CostoAdicional(concepto="Envío", valor=None)], "b": [CostoAdicional(concepto="Envío", valor=None)]}
    ops = armar_presupuestos([arroz, aceite], ev, {"a": "A", "b": "B"}, costos)
    comb = ops[-1]
    assert comb.viable and comb.subtotal_productos == 38000 and len(comb.costos_adicionales) == 2
    # A = 60000, B = 48000, combinada = 38000 -> ahorro 20.8 % > 5 %
    assert recomendar(ops).opcion == "Compra combinada"


def test_sustitutos_y_cobertura_parcial():
    arroz, aceite = _item(0), _item(1, "aceite", 2.0, "volumen", "L")
    pares = [
        (arroz, _oferta("Arroz integral 1 kg", 6000, "a", sku="1"), "sustituto"),
        (aceite, _oferta("Aceite 1 L", 9000, "b", sku="2"), "equivalente"),
    ]
    ev = _evaluar_todo([arroz, aceite], pares)
    costos = {"a": [], "b": []}
    op_a, op_b, comb = armar_presupuestos([arroz, aceite], ev, {"a": "A", "b": "B"}, costos)
    assert op_a.cobertura == "parcial" and op_a.items_sin_cotizacion == ["aceite"]
    assert comb.cobertura == "completa_con_sustitutos"
    sin_sust = armar_presupuestos([arroz, aceite], ev, {"a": "A", "b": "B"}, costos, permitir_sustitutos=False)
    assert sin_sust[-1].items_sin_cotizacion == ["arroz"]
