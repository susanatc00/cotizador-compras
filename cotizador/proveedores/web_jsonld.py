"""Proveedor web: lee datos estructurados schema.org (JSON-LD) de páginas públicas.

Mecanismo permitido por el enunciado: "Extracción de datos estructurados o páginas
web públicas, respetando las condiciones de uso".

Reglas que aplica este cliente:
1. Antes de cada URL consulta robots.txt del sitio (intérprete RFC 9309 propio, ver robots.py,
   que entiende los comodines `*` y `$` que usan las tiendas). Si la URL está prohibida, NO se descarga
   y el resultado queda como `bloqueado_robots`.
2. Solo pide páginas HTML públicas (la misma que ve un navegador). No usa endpoints
   internos (/api/, /_v/, /busca) ni simula sesiones, cookies o cabeceras de navegador.
3. Se identifica con un User-Agent propio y espera `intervalo_s` entre peticiones al
   mismo sitio (o el Crawl-delay del robots.txt si es mayor).
4. Guarda en memoria lo ya descargado durante la ejecución para no repetir peticiones.
"""
from __future__ import annotations

import hashlib
import html
import json
import re
import threading
import time
from urllib.parse import urljoin, urlparse

import httpx

from cotizador.proveedores.base import Proveedor, ahora_iso
from cotizador.proveedores.robots import RobotsTxt
from cotizador.schemas import Oferta, ResultadoConsulta
from cotizador.texto import slug_busqueda

USER_AGENT = "cotizador-academico/0.1 (proyecto de curso UPB; consulta puntual de precios publicos)"

_RE_JSONLD = re.compile(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', re.S | re.I)

_DISPONIBILIDAD = {
    "instock": "disponible",
    "limitedavailability": "disponible",
    "onlineonly": "disponible",
    "instoreonly": "disponible",
    "outofstock": "agotado",
    "soldout": "agotado",
    "discontinued": "agotado",
}


class _ClienteRespetuoso:
    """Cliente HTTP compartido: robots.txt + espera entre peticiones + caché."""

    def __init__(self, intervalo_s: float = 1.5, timeout_s: float = 25.0):
        self.intervalo_s = intervalo_s
        self._http = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Language": "es-CO,es;q=0.9"},
            timeout=timeout_s,
            follow_redirects=True,
        )
        self._robots: dict[str, RobotsTxt | None] = {}
        self._ultima: dict[str, float] = {}
        self._cache: dict[str, str] = {}
        self._locks: dict[str, threading.Lock] = {}
        self._lock_global = threading.Lock()

    def _lock_de(self, host: str) -> threading.Lock:
        with self._lock_global:
            return self._locks.setdefault(host, threading.Lock())

    def _robots_de(self, url: str) -> RobotsTxt | None:
        origen = "{0.scheme}://{0.netloc}".format(urlparse(url))
        if origen not in self._robots:
            try:
                r = self._http.get(origen + "/robots.txt")
                self._robots[origen] = RobotsTxt(r.text) if r.status_code == 200 else None
            except httpx.HTTPError:
                self._robots[origen] = None
        return self._robots[origen]

    def permitido(self, url: str) -> bool:
        robots = self._robots_de(url)
        if robots is None:
            # Sin robots.txt legible no hay restricciones publicadas; igual se limita la tasa.
            return True
        return robots.can_fetch(url, USER_AGENT)

    def olvidar(self, url: str) -> None:
        """Saca una URL de la caché (p. ej. si llegó sin los datos esperados)."""
        self._cache.pop(url, None)

    def get(self, url: str) -> tuple[int, str, str]:
        """Devuelve (status, url_final, html). Respeta tasa y usa caché."""
        host = urlparse(url).netloc
        with self._lock_de(host):
            if url in self._cache:
                return 200, url, self._cache[url]
            robots = self._robots_de(url)
            espera = self.intervalo_s
            if robots is not None and robots.crawl_delay(USER_AGENT):
                espera = max(espera, float(robots.crawl_delay(USER_AGENT)))
            transcurrido = time.monotonic() - self._ultima.get(host, 0.0)
            if transcurrido < espera:
                time.sleep(espera - transcurrido)
            r = self._http.get(url)
            self._ultima[host] = time.monotonic()
            if r.status_code == 200:
                self._cache[url] = r.text
            return r.status_code, str(r.url), r.text


_CLIENTE: _ClienteRespetuoso | None = None


def cliente_compartido() -> _ClienteRespetuoso:
    global _CLIENTE
    if _CLIENTE is None:
        _CLIENTE = _ClienteRespetuoso()
    return _CLIENTE


def extraer_productos_jsonld(pagina: str) -> list[dict]:
    """Devuelve los objetos schema.org `Product` presentes en la página."""
    productos: list[dict] = []
    for bloque in _RE_JSONLD.findall(pagina):
        try:
            data = json.loads(bloque)
        except json.JSONDecodeError:
            continue
        for nodo in data if isinstance(data, list) else [data]:
            if not isinstance(nodo, dict):
                continue
            tipo = nodo.get("@type")
            if tipo == "Product":
                productos.append(nodo)
            elif tipo == "ItemList":
                for elem in nodo.get("itemListElement", []):
                    item = elem.get("item", elem) if isinstance(elem, dict) else None
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        productos.append(item)
    return productos


def _precio_y_disponibilidad(offers: dict | list | None) -> tuple[float | None, str | None, str]:
    if not offers:
        return None, None, "desconocida"
    if isinstance(offers, list):
        offers = offers[0] if offers else {}
    moneda = offers.get("priceCurrency")
    precio = offers.get("lowPrice", offers.get("price"))
    sub = offers.get("offers") or []
    disp_raw = offers.get("availability")
    if not disp_raw and sub:
        disp_raw = sub[0].get("availability")
        moneda = moneda or sub[0].get("priceCurrency")
        precio = precio if precio is not None else sub[0].get("price")
    disp = "desconocida"
    if disp_raw:
        disp = _DISPONIBILIDAD.get(str(disp_raw).rsplit("/", 1)[-1].lower(), "desconocida")
    try:
        precio_f = float(precio) if precio is not None else None
    except (TypeError, ValueError):
        precio_f = None
    if precio_f is not None and precio_f <= 0:
        precio_f = None
    return precio_f, moneda, disp


def producto_a_oferta(prod: dict, prov: Proveedor, fuente: str, fecha: str, base_url: str) -> Oferta:
    precio, moneda, disp = _precio_y_disponibilidad(prod.get("offers"))
    marca = prod.get("brand")
    if isinstance(marca, dict):
        marca = marca.get("name")
    url = prod.get("@id") or prod.get("url")
    if url:
        url = urljoin(base_url, url)
    sku = str(prod.get("sku") or prod.get("mpn") or "") or None
    nombre = html.unescape(str(prod.get("name", ""))).strip()
    return Oferta(
        id=f"{prov.id}:{sku or hashlib.md5((url or nombre).encode()).hexdigest()[:10]}",
        proveedor_id=prov.id,
        proveedor=prov.nombre,
        nombre=nombre,
        marca=html.unescape(marca).strip() if isinstance(marca, str) else None,
        presentacion_texto=nombre,
        precio=precio,
        moneda=moneda,
        url=url,
        sku=sku,
        fecha_consulta=fecha,
        disponibilidad=disp,
        mecanismo=prov.mecanismo,
        fuente_consulta=fuente,
    )


class ProveedorWebJsonLd(Proveedor):
    mecanismo = "jsonld_publico"

    def __init__(self, cfg: dict, cliente: _ClienteRespetuoso | None = None):
        super().__init__(cfg)
        self.base_url: str = cfg["base_url"].rstrip("/")
        self.plantillas: list[str] = cfg.get("plantillas_busqueda", ["/{termino}?map=ft"])
        self.max_resultados: int = int(cfg.get("max_resultados", 30))
        self.sitemap_categorias: str | None = cfg.get("sitemap_categorias")
        self.prefijo_categorias: str = cfg.get("prefijo_categorias", "/")
        self.reintentos: int = int(cfg.get("reintentos_sin_datos", 1))
        self._cliente = cliente or cliente_compartido()
        self._categorias: list[str] | None = None

    def urls_busqueda(self, termino: str) -> list[str]:
        t = slug_busqueda(termino)
        if t.endswith("s"):
            plural = t
        elif t.endswith("z"):
            plural = t[:-1] + "ces"
        else:
            plural = t + ("s" if t[-1:] in "aeiou" else "es")
        urls = [self.base_url + p.format(termino=t, plural=plural) for p in self.plantillas]
        return list(dict.fromkeys(urls))  # sin duplicados, en orden

    def url_busqueda(self, termino: str) -> str:
        return self.urls_busqueda(termino)[0]

    # --- Vía 2: categoría encontrada en el sitemap público ---------------------
    def _lista_categorias(self) -> list[str]:
        if self._categorias is None:
            self._categorias = []
            if self.sitemap_categorias and self._cliente.permitido(self.sitemap_categorias):
                try:
                    status, _, xml = self._cliente.get(self.sitemap_categorias)
                    if status == 200:
                        self._categorias = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
                except httpx.HTTPError:
                    pass
        return self._categorias

    def url_categoria(self, termino: str) -> str | None:
        """Categoría del sitemap cuyo último tramo contiene el término (o su plural)."""
        t = slug_busqueda(termino)
        formas = {t, t + "s", t + "es"}
        candidatas = []
        for url in self._lista_categorias():
            ruta = urlparse(url).path
            if not ruta.startswith(self.prefijo_categorias):
                continue
            ultimo = ruta.rstrip("/").rsplit("/", 1)[-1]
            if formas & set(ultimo.split("-")):
                candidatas.append(url)
        # La más específica primero: la que empieza por el término y tiene la ruta más corta.
        candidatas.sort(key=lambda u: (not urlparse(u).path.rsplit("/", 1)[-1].startswith(t), len(u)))
        return candidatas[0] if candidatas else None

    def _leer(self, url: str, fecha: str) -> tuple[str, list[Oferta], str]:
        """Devuelve (estado, ofertas, mensaje) para una URL concreta."""
        if not self._cliente.permitido(url):
            return "bloqueado_robots", [], f"robots.txt no permite {url}; no se descargó."
        productos: list[dict] = []
        for _ in range(1 + self.reintentos):
            status, url_final, pagina = self._cliente.get(url)
            if status in (404, 410):
                return "ok", [], f"{url} no existe (HTTP {status})"
            if status != 200:
                return "error", [], f"HTTP {status} en {url}"
            productos = extraer_productos_jsonld(pagina)[: self.max_resultados]
            if productos:
                break
            # Las tiendas a veces sirven la página sin el bloque JSON-LD (render
            # del servidor incompleto): se descarta de la caché y se reintenta una vez.
            self._cliente.olvidar(url)
        return "ok", [producto_a_oferta(p, self, url_final, fecha, self.base_url) for p in productos], ""

    def buscar(self, termino: str, item_idx: int) -> ResultadoConsulta:
        """Vía 1: páginas públicas de búsqueda (`plantillas_busqueda`, en orden).
        Vía 2 (si ninguna trae productos): página de la categoría correspondiente
        según el sitemap público."""
        fecha = ahora_iso()
        urls = self.urls_busqueda(termino)
        url = urls[0]
        base = dict(proveedor_id=self.id, proveedor=self.nombre, item_idx=item_idx, termino=termino,
                    fecha_consulta=fecha)
        notas = []
        ultimo_estado = "sin_resultados"
        try:
            for u in urls:
                estado, ofertas, msg = self._leer(u, fecha)
                if estado == "ok" and ofertas:
                    return ResultadoConsulta(**base, url_consulta=u, estado="ok", ofertas=ofertas,
                                             mensaje=f"{len(ofertas)} productos leídos del JSON-LD de la búsqueda "
                                                     "pública" + (f" ({'; '.join(notas)})" if notas else "") + ".")
                notas.append(msg or f"{u} no publica productos en JSON-LD")
                ultimo_estado = estado if estado != "ok" else ultimo_estado
            url_cat = self.url_categoria(termino)
            if url_cat:
                estado2, ofertas2, msg2 = self._leer(url_cat, fecha)
                if estado2 == "ok" and ofertas2:
                    return ResultadoConsulta(**base, url_consulta=url_cat, estado="ok", ofertas=ofertas2,
                                             mensaje=f"{len(ofertas2)} productos del JSON-LD de la categoría "
                                                     f"(sitemap); {'; '.join(notas)}.")
                notas.append(msg2 or f"la categoría {url_cat} no publica productos")
                ultimo_estado = estado2 if estado2 != "ok" else ultimo_estado
        except httpx.HTTPError as e:
            return ResultadoConsulta(**base, url_consulta=url, estado="error", mensaje=f"Error de red: {type(e).__name__}")
        estado_final = ultimo_estado if ultimo_estado in ("error", "bloqueado_robots") else "sin_resultados"
        return ResultadoConsulta(**base, url_consulta=url, estado=estado_final, mensaje="; ".join(notas))
