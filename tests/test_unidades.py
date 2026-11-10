import pytest

from cotizador.unidades import cantidad_en_dimension, leer_presentacion, unidad_solicitud_a_base

# Nombres reales tomados de Olímpica y Jumbo (2026-11-09).
CASOS = [
    ("Arroz Diana blanco x5kg", 5.0, "masa", False),
    ("Arroz Diana Premium blanco x4000g", 4.0, "masa", False),
    ("Arroz Castellano Premium blanco x2.5kg", 2.5, "masa", False),
    ("ARROZ TAILANDES 2.500 G", 2.5, "masa", False),  # punto = separador de miles en gramos
    ("Arroz Diana Paca 12,5 Kg X25 Unds", 12.5, "masa", True),  # medida total (umbral)
    ("Arroz Olimpica Paca Económica 500 G X25 Unds", 12.5, "masa", True),  # 25 x 500 g
    ("Arroz Diana blanco x25und x500g c-u", 12.5, "masa", False),  # c/u explícito
    ("Aceite Medalla de Oro Mezcla 3 Lt", 3.0, "volumen", False),
    ("Aceite Premier girasol x2700ml", 2.7, "volumen", False),
    ("Leche Entera Colanta Bolsa x6 Und x1000ml", 6.0, "volumen", True),
    ("AZUCAR MANUELITA DORADA NATURAL X 907 G", 0.907, "masa", False),
    ("Papel higiénico Familia Acolchamax extra grande x12 rollos", 12.0, "unidad", False),
    ("Huevo Kikes Rojo AA X30 Unds", 30.0, "unidad", False),
]


@pytest.mark.parametrize("nombre,cantidad,dimension,inferida", CASOS)
def test_leer_presentacion(nombre, cantidad, dimension, inferida):
    p = leer_presentacion(nombre)
    assert p.cantidad_base == pytest.approx(cantidad)
    assert p.dimension == dimension
    assert p.inferida is inferida


def test_presentacion_ilegible():
    p = leer_presentacion("Jabón Rey")
    assert p.cantidad_base is None and p.dimension is None


def test_masa_y_volumen_no_se_convierten():
    p = leer_presentacion("Endulzante Stevia liquido x250ml")
    assert cantidad_en_dimension(p, "masa") == (None, None)


def test_pedido_por_unidades_de_un_empaque_en_masa():
    cant, sup = cantidad_en_dimension(leer_presentacion("Jabón Rey 300 g"), "unidad")
    assert cant == 1.0 and "1 unidad" in sup
    cant, sup = cantidad_en_dimension(leer_presentacion("Atún Van Camps 160 g x3 und"), "unidad")
    assert cant == 3.0 and sup is None


@pytest.mark.parametrize("cantidad,unidad,esperado,dim", [
    (500, "g", 0.5, "masa"), (3, "lb", 1.5, "masa"), (750, "mL", 0.75, "volumen"), (12, "und", 12, "unidad"),
])
def test_unidad_solicitud_a_base(cantidad, unidad, esperado, dim):
    base, d, _ = unidad_solicitud_a_base(cantidad, unidad)
    assert base == pytest.approx(esperado) and d == dim
