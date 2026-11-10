"""robots.txt (RFC 9309), extracción JSON-LD y selección de proveedores."""
from pathlib import Path

from cotizador.proveedores import seleccionar_proveedores
from cotizador.proveedores.catalogo import ProveedorCatalogo
from cotizador.proveedores.robots import RobotsTxt
from cotizador.proveedores.web_jsonld import ProveedorWebJsonLd, extraer_productos_jsonld

UA = "cotizador-academico/0.1"

# Extracto del robots.txt de olimpica.com (2026-11-09)
ROBOTS_OLIMPICA = """
User-agent: *
Disallow: /secret
Disallow: /*?orderFormId=
Allow: /*.css

User-agent: *
Disallow: /*&
Disallow: /*%

User-agent: Googlebot
Disallow: /busca/*
"""

# Extracto del robots.txt de jumbocolombia.com (2026-11-09)
ROBOTS_JUMBO = """
User-agent: *
Disallow: /busca*
Disallow: /checkout/
Disallow: /?*ft
Disallow: /?*map
"""


def test_robots_olimpica_porcentaje_es_literal():
    r = RobotsTxt(ROBOTS_OLIMPICA)
    assert r.can_fetch("https://www.olimpica.com/aceite?map=ft", UA)
    assert not r.can_fetch("https://www.olimpica.com/arroz%20diana", UA)
    assert not r.can_fetch("https://www.olimpica.com/a?x=1&y=2", UA)
    assert not r.can_fetch("https://www.olimpica.com/c?orderFormId=9", UA)
    # Las reglas de Googlebot no aplican a otros agentes.
    assert r.can_fetch("https://www.olimpica.com/busca/arroz", UA)


def test_robots_jumbo():
    r = RobotsTxt(ROBOTS_JUMBO)
    assert r.can_fetch("https://www.jumbocolombia.com/arroz?map=ft", UA)
    assert not r.can_fetch("https://www.jumbocolombia.com/busca?ft=arroz", UA)
    assert not r.can_fetch("https://www.jumbocolombia.com/?ft=arroz", UA)
    assert not r.can_fetch("https://www.jumbocolombia.com/checkout/cart", UA)


def test_robots_regla_mas_larga_y_empate_allow():
    r = RobotsTxt("User-agent: *\nDisallow: /a\nAllow: /a/b\nDisallow: /x$\nAllow: /y\nDisallow: /y\n")
    assert not r.can_fetch("https://s/a/c", UA)
    assert r.can_fetch("https://s/a/b/c", UA)
    assert not r.can_fetch("https://s/x", UA)
    assert r.can_fetch("https://s/xy", UA)  # '$' ancla el final
    assert r.can_fetch("https://s/y", UA)  # empate -> Allow


def test_robots_grupo_especifico_gana_a_asterisco():
    r = RobotsTxt("User-agent: *\nDisallow: /\n\nUser-agent: cotizador-academico\nAllow: /\n")
    assert r.can_fetch("https://s/lo-que-sea", UA)
    assert not r.can_fetch("https://s/lo-que-sea", "OtroBot/1.0")


PAGINA = """<html><head>
<script type="application/ld+json">{"@type":"BreadcrumbList","itemListElement":[]}</script>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"ItemList","itemListElement":[
 {"@type":"ListItem","position":1,"item":{"@type":"Product","@id":"https://t.example/arroz-x5kg/p","name":"Arroz Diana blanco x5kg",
  "brand":{"@type":"Brand","name":"DIANA"},"sku":"13396","offers":{"@type":"AggregateOffer","lowPrice":21990,"priceCurrency":"COP",
  "offers":[{"@type":"Offer","price":21990,"availability":"http://schema.org/InStock"}]}}},
 {"@type":"ListItem","position":2,"item":{"@type":"Product","@id":"/aceite-x3l/p","name":"Aceite Cuisine&amp;Co x3000Ml",
  "sku":"9","offers":{"@type":"AggregateOffer","lowPrice":0,"priceCurrency":"COP",
  "offers":[{"@type":"Offer","availability":"http://schema.org/OutOfStock"}]}}}
]}</script></head></html>"""


class _ClienteFalso:
    def __init__(self, paginas, prohibidas=()):
        self.paginas, self.prohibidas, self.pedidas = paginas, set(prohibidas), []

    def permitido(self, url):
        return url not in self.prohibidas

    def olvidar(self, url):
        pass

    def get(self, url):
        self.pedidas.append(url)
        pagina = self.paginas.get(url)
        if isinstance(pagina, list):  # respuestas sucesivas para la misma URL
            pagina = pagina.pop(0) if len(pagina) > 1 else pagina[0]
        return (200, url, pagina) if pagina is not None else (404, url, "")


def _prov(cliente, **extra):
    cfg = {"id": "t", "nombre": "Tienda", "base_url": "https://t.example", **extra}
    return ProveedorWebJsonLd(cfg, cliente=cliente)


def test_extraer_productos_jsonld():
    assert [p["name"] for p in extraer_productos_jsonld(PAGINA)] == [
        "Arroz Diana blanco x5kg", "Aceite Cuisine&amp;Co x3000Ml"]


def test_proveedor_web_conserva_trazabilidad():
    cli = _ClienteFalso({"https://t.example/arroz?map=ft": PAGINA})
    r = _prov(cli).buscar("arroz", 0)
    assert r.estado == "ok" and len(r.ofertas) == 2
    o1, o2 = r.ofertas
    assert (o1.precio, o1.moneda, o1.disponibilidad, o1.marca) == (21990, "COP", "disponible", "DIANA")
    assert o1.url == "https://t.example/arroz-x5kg/p" and o1.fecha_consulta and o1.fuente_consulta
    assert o2.nombre == "Aceite Cuisine&Co x3000Ml"  # entidades HTML decodificadas
    assert o2.url == "https://t.example/aceite-x3l/p"  # URL relativa resuelta
    assert o2.precio is None and o2.disponibilidad == "agotado"  # precio 0 = sin precio


def test_reintenta_si_la_pagina_llega_sin_jsonld():
    url = "https://t.example/arroz?map=ft"
    cli = _ClienteFalso({url: ["<html>render incompleto</html>", PAGINA]})
    r = _prov(cli).buscar("arroz", 0)
    assert r.estado == "ok" and cli.pedidas == [url, url]


def test_proveedor_web_respeta_robots():
    cli = _ClienteFalso({"https://t.example/arroz?map=ft": PAGINA}, prohibidas={"https://t.example/arroz?map=ft"})
    r = _prov(cli).buscar("arroz", 0)
    assert r.estado == "bloqueado_robots" and cli.pedidas == []


def test_proveedor_web_usa_categoria_del_sitemap():
    sitemap = ("<urlset><url><loc>https://t.example/supermercado/despensa/aceites</loc></url>"
               "<url><loc>https://t.example/belleza/aceites-corporales</loc></url></urlset>")
    cli = _ClienteFalso({
        "https://t.example/aceite?map=ft": "<html>sin productos</html>",
        "https://t.example/sitemap/category-0.xml": sitemap,
        "https://t.example/supermercado/despensa/aceites": PAGINA,
    })
    p = _prov(cli, sitemap_categorias="https://t.example/sitemap/category-0.xml", prefijo_categorias="/supermercado/")
    r = p.buscar("aceite", 1)
    assert r.estado == "ok" and r.url_consulta.endswith("/supermercado/despensa/aceites")
    assert "categoría" in r.mensaje


def test_catalogo_filtra_por_proveedor_y_termino():
    cfg = {"id": "tienda_b", "nombre": "Tienda B", "catalogo": str(Path(__file__).parent / "fixtures" / "catalogo_prueba.csv")}
    r = ProveedorCatalogo(cfg).buscar("aceite", 1)
    assert r.estado == "ok"
    assert {o.sku for o in r.ofertas} == {"B3", "B4", "B5"}
    assert all(o.mecanismo == "catalogo_referencia" for o in r.ofertas)


def test_seleccion_de_proveedores(registro):
    ids, sup = seleccionar_proveedores(registro, [], 2)
    assert ids == ["tienda_a", "tienda_b"] and "no nombró" in sup[0]

    ids, sup = seleccionar_proveedores(registro, ["Éxito", "tienda b"], None)
    assert ids == ["tienda_b", "tienda_a"]
    assert any("Éxito" in s and "endpoints internos" in s for s in sup)
