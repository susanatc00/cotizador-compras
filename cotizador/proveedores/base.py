"""Contrato común de los proveedores (herramientas programáticas del grafo)."""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime, timezone

from cotizador.schemas import CostoAdicional, ResultadoConsulta


def ahora_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Proveedor(ABC):
    """Un proveedor sabe buscar un término y describir sus condiciones comerciales."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.id: str = cfg["id"]
        self.nombre: str = cfg["nombre"]
        self.alias: list[str] = cfg.get("alias", [])

    mecanismo: str = "desconocido"

    @abstractmethod
    def buscar(self, termino: str, item_idx: int) -> ResultadoConsulta:
        """Devuelve las ofertas encontradas para `termino` (nunca lanza excepción)."""

    def costos_adicionales(self) -> list[CostoAdicional]:
        """Costos conocidos o desconocidos (valor=None) de comprar en este proveedor."""
        envio = self.cfg.get("envio", {})
        return [
            CostoAdicional(
                concepto="Envío a domicilio",
                valor=envio.get("valor"),  # None si no se conoce: NUNCA se asume 0
                fuente=envio.get("fuente", ""),
                nota=envio.get("nota", ""),
            )
        ]

    def condiciones(self, ciudad: str) -> list[str]:
        """Condiciones comerciales que el sistema no puede verificar."""
        return [c.format(ciudad=ciudad) for c in self.cfg.get("condiciones_no_verificadas", [])]
