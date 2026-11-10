"""Intérprete mínimo de robots.txt según RFC 9309 (Robots Exclusion Protocol).

Por qué no se usa `urllib.robotparser` ni `protego`:
- `urllib.robotparser` no entiende los comodines `*` y `$`, que las tiendas usan
  (p. ej. `Disallow: /*?orderFormId=`), así que permitiría URLs prohibidas.
- `protego` trata la regla `Disallow: /*%` (que publica olimpica.com) como si
  bloqueara cualquier URL con `?`. En RFC 9309 `%` es un carácter literal: esa
  regla solo afecta URLs que contengan `%`.

Reglas implementadas:
- Se usan los grupos cuyo `User-agent` coincide con el token del cliente; si no hay,
  los grupos `*`. Varios grupos para el mismo agente se combinan.
- Una regla coincide si su patrón es prefijo de "ruta?consulta"; `*` = cualquier
  secuencia, `$` al final = fin de URL.
- Gana la regla que coincide con el patrón más largo; en empate gana `Allow`.
- `Crawl-delay` (no estándar) se lee si existe.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urlparse


@dataclass
class _Grupo:
    agentes: list[str] = field(default_factory=list)
    reglas: list[tuple[bool, str]] = field(default_factory=list)  # (permitir, patrón)
    crawl_delay: float | None = None


def _patron_a_regex(patron: str) -> re.Pattern:
    ancla = patron.endswith("$")
    if ancla:
        patron = patron[:-1]
    cuerpo = ".*".join(re.escape(p) for p in patron.split("*"))
    return re.compile(cuerpo + ("$" if ancla else ""), re.S)


class RobotsTxt:
    def __init__(self, texto: str):
        self.grupos: list[_Grupo] = []
        actual: _Grupo | None = None
        leyendo_agentes = False
        for linea in texto.splitlines():
            linea = linea.split("#", 1)[0].strip()
            if ":" not in linea:
                continue
            clave, valor = (s.strip() for s in linea.split(":", 1))
            clave = clave.lower()
            if clave == "user-agent":
                if actual is None or not leyendo_agentes:
                    actual = _Grupo()
                    self.grupos.append(actual)
                actual.agentes.append(valor.lower())
                leyendo_agentes = True
            elif clave in ("allow", "disallow") and actual is not None:
                leyendo_agentes = False
                if valor:  # "Disallow:" vacío no restringe nada
                    actual.reglas.append((clave == "allow", valor))
            elif clave == "crawl-delay" and actual is not None:
                leyendo_agentes = False
                try:
                    actual.crawl_delay = float(valor)
                except ValueError:
                    pass

    def _grupos_para(self, agente: str) -> list[_Grupo]:
        token = agente.split("/", 1)[0].strip().lower()
        propios = [g for g in self.grupos if token in g.agentes]
        return propios or [g for g in self.grupos if "*" in g.agentes]

    def can_fetch(self, url: str, agente: str) -> bool:
        u = urlparse(url)
        ruta = (u.path or "/") + (f"?{u.query}" if u.query else "")
        mejor: tuple[int, bool] | None = None  # (longitud, permitir)
        for g in self._grupos_para(agente):
            for permitir, patron in g.reglas:
                if _patron_a_regex(patron).match(ruta):
                    cand = (len(patron), permitir)
                    if mejor is None or cand[0] > mejor[0] or (cand[0] == mejor[0] and permitir):
                        mejor = cand
        return True if mejor is None else mejor[1]

    def crawl_delay(self, agente: str) -> float | None:
        delays = [g.crawl_delay for g in self._grupos_para(agente) if g.crawl_delay]
        return max(delays) if delays else None
