"""Utilidades de texto: normalización para comparar nombres de productos."""
import html
import re
import unicodedata


def sin_tildes(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def normalizar(texto: str | None) -> str:
    """minúsculas, sin tildes, sin entidades HTML y con espacios simples."""
    if not texto:
        return ""
    t = sin_tildes(html.unescape(texto)).lower()
    t = re.sub(r"[^a-z0-9.,/ ]+", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def contiene_palabra(texto_norm: str, palabra: str) -> bool:
    """¿`palabra` (o su plural/singular simple) aparece como palabra completa?"""
    p = normalizar(palabra)
    if not p:
        return False
    variantes = {p, p + "s", p + "es"}
    if p.endswith("es"):
        variantes.add(p[:-2])
    if p.endswith("s"):
        variantes.add(p[:-1])
    patron = r"(?<![a-z])(" + "|".join(re.escape(v) for v in variantes) + r")(?![a-z])"
    return re.search(patron, texto_norm) is not None


def cop(valor: float | None) -> str:
    """Formato de pesos colombianos: $52.170 (punto como separador de miles)."""
    if valor is None:
        return "desconocido"
    return "$" + f"{valor:,.0f}".replace(",", ".")


def slug_busqueda(termino: str) -> str:
    """Término de búsqueda seguro para la URL: ASCII, minúsculas, sin espacios ni '%'."""
    t = normalizar(termino)
    t = re.sub(r"[^a-z0-9]+", "-", t).strip("-")
    return t
