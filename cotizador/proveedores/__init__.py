"""Registro de proveedores y selección según la solicitud del usuario."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cotizador.proveedores.base import Proveedor
from cotizador.proveedores.catalogo import ProveedorCatalogo
from cotizador.proveedores.web_jsonld import ProveedorWebJsonLd
from cotizador.schemas import ResultadoConsulta
from cotizador.texto import normalizar

RAIZ = Path(__file__).resolve().parents[2]
CONFIG_PROVEEDORES = RAIZ / "config" / "proveedores.json"

Fuente = Literal["web", "catalogo", "auto"]


class ProveedorConRespaldo(Proveedor):
    """Consulta la web y, si falla por red/HTTP, usa el catálogo de referencia.

    El resultado conserva el mecanismo real usado en cada oferta, así el reporte
    distingue un precio consultado hoy de uno tomado del catálogo de referencia.
    """

    def __init__(self, web: ProveedorWebJsonLd, respaldo: ProveedorCatalogo):
        super().__init__(web.cfg)
        self.web = web
        self.respaldo = respaldo
        self.mecanismo = web.mecanismo

    def buscar(self, termino: str, item_idx: int) -> ResultadoConsulta:
        r = self.web.buscar(termino, item_idx)
        if r.estado != "error":
            return r
        r2 = self.respaldo.buscar(termino, item_idx)
        r2.mensaje = f"Consulta web falló ({r.mensaje}); se usó el catálogo de referencia. " + r2.mensaje
        return r2


@dataclass
class Registro:
    config: dict
    proveedores: dict[str, Proveedor]
    no_soportados: dict[str, dict] = field(default_factory=dict)

    @property
    def ciudad_por_defecto(self) -> str:
        return self.config.get("ciudad_por_defecto", "Medellín")

    @property
    def envio_estimado(self) -> tuple[float | None, str]:
        """(valor por pedido, nota) del envío estimado configurado, o (None, '')."""
        cfg = self.config.get("envio_estimado") or {}
        return cfg.get("valor_por_pedido"), cfg.get("nota", "")

    def buscar_id(self, nombre: str) -> tuple[str | None, str | None]:
        """Devuelve (id_soportado, motivo_si_no_soportado)."""
        n = normalizar(nombre)
        for pid, prov in self.proveedores.items():
            if n == pid or n in {normalizar(a) for a in prov.alias} or normalizar(prov.nombre) == n:
                return pid, None
        for pid, info in self.no_soportados.items():
            if n == pid or n in {normalizar(a) for a in info.get("alias", [])}:
                return None, info["motivo"]
        return None, "no está configurado en config/proveedores.json"


def construir_proveedor(cfg: dict, fuente: Fuente) -> Proveedor:
    tipo = cfg.get("tipo")
    if tipo == "catalogo":
        return ProveedorCatalogo(cfg, raiz=RAIZ)
    if tipo == "web_jsonld":
        web = ProveedorWebJsonLd(cfg)
        respaldo_ruta = cfg.get("catalogo_respaldo")
        if fuente == "web" or not respaldo_ruta:
            return web
        respaldo = ProveedorCatalogo({**cfg, "catalogo": respaldo_ruta}, raiz=RAIZ)
        return respaldo if fuente == "catalogo" else ProveedorConRespaldo(web, respaldo)
    raise ValueError(f"Tipo de proveedor desconocido: {tipo}")


def cargar_registro(fuente: Fuente = "auto", ruta: Path = CONFIG_PROVEEDORES) -> Registro:
    config = json.loads(ruta.read_text(encoding="utf-8"))
    provs = {c["id"]: construir_proveedor(c, fuente) for c in config["proveedores"]}
    return Registro(config=config, proveedores=provs, no_soportados=config.get("no_soportados", {}))


def seleccionar_proveedores(
    registro: Registro, mencionados: list[str], cantidad: int | None
) -> tuple[list[str], list[str]]:
    """Decide qué proveedores consultar. Devuelve (ids, suposiciones explícitas)."""
    suposiciones: list[str] = []
    elegidos: list[str] = []
    for nombre in mencionados:
        pid, motivo = registro.buscar_id(nombre)
        if pid and pid not in elegidos:
            elegidos.append(pid)
        elif motivo:
            suposiciones.append(f"No se consulta «{nombre}»: {motivo}")

    disponibles = [p for p in registro.proveedores if p not in elegidos]
    minimo = max(2, cantidad or 0)
    if len(elegidos) < minimo and disponibles:
        faltan = min(minimo - len(elegidos), len(disponibles))
        agregados = disponibles[:faltan]
        elegidos += agregados
        nombres = ", ".join(registro.proveedores[p].nombre for p in agregados)
        if mencionados:
            suposiciones.append(f"Para comparar al menos {minimo} proveedores se agregó: {nombres}.")
        else:
            suposiciones.append(f"El usuario no nombró proveedores; se consultan: "
                                f"{', '.join(registro.proveedores[p].nombre for p in elegidos)}.")
    if cantidad and cantidad > len(registro.proveedores):
        suposiciones.append(f"Se pidieron {cantidad} proveedores pero solo hay {len(registro.proveedores)} "
                            "con acceso permitido a precios públicos.")
    return elegidos, suposiciones


__all__ = [
    "Proveedor", "ProveedorCatalogo", "ProveedorWebJsonLd", "ProveedorConRespaldo",
    "Registro", "cargar_registro", "seleccionar_proveedores", "construir_proveedor", "RAIZ",
]
