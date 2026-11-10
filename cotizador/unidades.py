"""Normalización de unidades y lectura de la presentación desde el nombre del producto.

Unidades base:  masa -> kg   |   volumen -> L   |   conteo -> und   |   longitud -> m

Los supermercados no publican el contenido del empaque como dato estructurado:
viene dentro del nombre ("Arroz Diana blanco x5kg", "Aceite ... 2700 Ml",
"Arroz Diana Paca 12,5 Kg X25 Unds", "Leche ... 1L x6unds"). Este módulo lo extrae
con reglas explícitas y documentadas; si no puede, devuelve una presentación vacía
y la oferta se marcará como NO COMPARABLE.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from cotizador.schemas import Dimension, Presentacion
from cotizador.texto import normalizar

# unidad (como aparece en el texto) -> (dimensión, factor a unidad base)
_UNIDADES: dict[str, tuple[Dimension, float]] = {
    "kg": ("masa", 1.0), "kgs": ("masa", 1.0), "kilo": ("masa", 1.0), "kilos": ("masa", 1.0),
    "kilogramo": ("masa", 1.0), "kilogramos": ("masa", 1.0),
    "g": ("masa", 0.001), "gr": ("masa", 0.001), "grs": ("masa", 0.001), "gramo": ("masa", 0.001),
    "gramos": ("masa", 0.001),
    "mg": ("masa", 1e-6),
    "lb": ("masa", 0.45359237), "lbs": ("masa", 0.45359237), "libra": ("masa", 0.45359237),
    "libras": ("masa", 0.45359237),
    "oz": ("masa", 0.028349523125),
    "l": ("volumen", 1.0), "lt": ("volumen", 1.0), "lts": ("volumen", 1.0), "litro": ("volumen", 1.0),
    "litros": ("volumen", 1.0),
    "ml": ("volumen", 0.001), "cc": ("volumen", 0.001), "mililitros": ("volumen", 0.001),
    "m": ("longitud", 1.0), "mt": ("longitud", 1.0), "mts": ("longitud", 1.0), "metros": ("longitud", 1.0),
}

UNIDAD_BASE: dict[Dimension, str] = {"masa": "kg", "volumen": "L", "unidad": "und", "longitud": "m"}

# Unidades que entrega el intérprete (Literal UnidadSolicitud) -> (dimensión, factor)
_UNIDADES_SOLICITUD: dict[str, tuple[Dimension, float]] = {
    "kg": ("masa", 1.0), "g": ("masa", 0.001), "lb": ("masa", 0.5),  # libra colombiana = 500 g
    "L": ("volumen", 1.0), "mL": ("volumen", 0.001), "und": ("unidad", 1.0),
}

_PALABRAS_CONTEO = r"(?:und|unds|unid|unidad|unidades|un|u|rollos?|paquetes?|bolsas?|sobres?|latas?|botellas?|tarros?|barras?|pastillas?)"
_NUM = r"(\d+(?:[.,]\d+)?)"

_RE_MEDIDA = re.compile(
    r"(?<![a-z0-9])(?:x\s*)?" + _NUM + r"\s*(" + "|".join(sorted(map(re.escape, _UNIDADES), key=len, reverse=True)) + r")(?![a-z])"
)
_RE_CONTEO = re.compile(r"(?<![a-z0-9])x?\s*(\d+)\s*" + _PALABRAS_CONTEO + r"(?![a-z])")
_RE_PACK = re.compile(r"(?:pack|paca|six pack|sixpack)\s*x?\s*(\d+)(?![\d.,]*\s*(?:kg|g|gr|grs|ml|l|lt)\b)")
_RE_X_SUELTO = re.compile(r"(?<![a-z0-9])x\s*(\d+)\s*$")  # "Huevos AA x30"
_RE_POR_UNIDAD = re.compile(r"(?<![a-z])(c/u|c u|cada uno|cada una|c-u|cu)(?![a-z])")

# Si contenido_por_unidad * unidades supera este umbral (kg o L), se asume que
# la medida leída es el TOTAL del empaque y no el contenido de cada unidad.
# Ej.: "Arroz Diana Paca 12,5 Kg X25 Unds" -> 12.5 kg (no 312.5 kg)
#      "Arroz ... Paca 500 G X25 Unds"     -> 25 x 0.5 kg = 12.5 kg
UMBRAL_TOTAL_PLAUSIBLE = 50.0


def _a_float(numero: str, unidad: str) -> float:
    """Convierte '2.500' / '2,5' / '12,5' a float.

    En Colombia el punto suele separar miles ('2.500 G' = 2500 g) y la coma los
    decimales ('12,5 Kg'). Pero también se ve '2.5kg'. Regla: un punto seguido de
    exactamente tres dígitos en unidades pequeñas (g, ml, cc) es separador de miles.
    """
    if "," in numero:
        return float(numero.replace(".", "").replace(",", "."))
    if "." in numero:
        entero, dec = numero.split(".", 1)
        if len(dec) == 3 and unidad in {"g", "gr", "grs", "gramos", "gramo", "ml", "cc", "mg"}:
            return float(entero + dec)
        return float(numero)
    return float(numero)


@dataclass
class _Medida:
    valor_base: float
    dimension: Dimension
    texto: str
    pos: int


def _medidas(t: str) -> list[_Medida]:
    salida = []
    for m in _RE_MEDIDA.finditer(t):
        unidad = m.group(2)
        dim, factor = _UNIDADES[unidad]
        valor = _a_float(m.group(1), unidad)
        if valor <= 0:
            continue
        texto = f"{m.group(1)} {unidad}"
        salida.append(_Medida(valor * factor, dim, texto, m.start()))
    return salida


def _conteo(t: str) -> int | None:
    m = _RE_CONTEO.search(t) or _RE_PACK.search(t) or _RE_X_SUELTO.search(t)
    if m:
        n = int(m.group(1))
        return n if n > 0 else None
    return None


def leer_presentacion(nombre: str) -> Presentacion:
    """Extrae el contenido del empaque a partir del nombre del producto."""
    t = normalizar(nombre)
    medidas = [m for m in _medidas(t) if m.dimension != "longitud"]
    longitudes = [m for m in _medidas(t) if m.dimension == "longitud"]
    conteo = _conteo(t)

    if medidas:
        principal = medidas[0]
        if conteo and conteo > 1:
            por_unidad_explicito = _RE_POR_UNIDAD.search(t) is not None
            total_si_por_unidad = principal.valor_base * conteo
            if por_unidad_explicito or total_si_por_unidad <= UMBRAL_TOTAL_PLAUSIBLE:
                return Presentacion(
                    cantidad_base=total_si_por_unidad,
                    dimension=principal.dimension,
                    unidades_por_empaque=conteo,
                    texto=f"{conteo} x {principal.texto} = {_fmt(total_si_por_unidad)} {UNIDAD_BASE[principal.dimension]}",
                    inferida=not por_unidad_explicito,
                )
            return Presentacion(
                cantidad_base=principal.valor_base,
                dimension=principal.dimension,
                unidades_por_empaque=conteo,
                texto=f"{principal.texto} en total ({conteo} unidades)",
                inferida=True,
            )
        return Presentacion(
            cantidad_base=principal.valor_base,
            dimension=principal.dimension,
            texto=f"{principal.texto} = {_fmt(principal.valor_base)} {UNIDAD_BASE[principal.dimension]}",
        )

    if conteo:
        texto = f"{conteo} und"
        if longitudes:
            texto += f" ({longitudes[0].texto} c/u)"
        return Presentacion(cantidad_base=float(conteo), dimension="unidad", unidades_por_empaque=conteo, texto=texto)

    return Presentacion(texto="no se pudo leer la presentación")


def cantidad_en_dimension(p: Presentacion, dimension: Dimension) -> tuple[float | None, str | None]:
    """Cantidad del empaque expresada en la dimensión pedida.

    Devuelve (cantidad, suposición). Si no es convertible -> (None, None).
    """
    if p.cantidad_base is None or p.dimension is None:
        return None, None
    if p.dimension == dimension:
        return p.cantidad_base, None
    if dimension == "unidad":
        # Se pidió por unidades y el empaque está en masa/volumen.
        if p.unidades_por_empaque:
            return float(p.unidades_por_empaque), None
        return 1.0, "se asume que el empaque es 1 unidad"
    return None, None  # masa <-> volumen no es convertible sin densidad


def unidad_solicitud_a_base(cantidad: float, unidad: str) -> tuple[float, Dimension, str]:
    dim, factor = _UNIDADES_SOLICITUD[unidad]
    return cantidad * factor, dim, UNIDAD_BASE[dim]


def _fmt(x: float) -> str:
    return f"{x:.3f}".rstrip("0").rstrip(".")


def fmt_cantidad(valor: float, unidad_base: str) -> str:
    return f"{_fmt(valor)} {unidad_base}"
