"""Proveedor desde catálogo CSV/JSON (datos de referencia).

Mecanismo permitido por el enunciado: "Importación de catálogos CSV, JSON o Excel,
identificados como datos de referencia". Cada oferta conserva la URL y la fecha
de captura originales; el reporte las marca como "catálogo de referencia" para
que no se confundan con una consulta en vivo.

Columnas esperadas (CSV o lista de objetos JSON):
    proveedor_id, proveedor, nombre, marca, precio, moneda, url, sku,
    disponibilidad, fecha_consulta, fuente_consulta
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from cotizador.proveedores.base import Proveedor, ahora_iso
from cotizador.schemas import Oferta, ResultadoConsulta
from cotizador.texto import contiene_palabra, normalizar


def leer_catalogo(ruta: Path) -> list[dict]:
    if ruta.suffix.lower() == ".json":
        return json.loads(ruta.read_text(encoding="utf-8"))
    with ruta.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


class ProveedorCatalogo(Proveedor):
    mecanismo = "catalogo_referencia"

    def __init__(self, cfg: dict, raiz: Path | None = None):
        super().__init__(cfg)
        ruta = Path(cfg["catalogo"])
        if not ruta.is_absolute() and raiz is not None:
            ruta = raiz / ruta
        self.ruta = ruta
        self._filas: list[dict] | None = None

    def _datos(self) -> list[dict]:
        if self._filas is None:
            filas = leer_catalogo(self.ruta)
            # Un catálogo puede traer varios proveedores: se filtra por id.
            self._filas = [f for f in filas if f.get("proveedor_id", self.id) == self.id]
        return self._filas

    def buscar(self, termino: str, item_idx: int) -> ResultadoConsulta:
        base = dict(proveedor_id=self.id, proveedor=self.nombre, item_idx=item_idx, termino=termino,
                    url_consulta=str(self.ruta.name), fecha_consulta=ahora_iso())
        try:
            filas = self._datos()
        except (OSError, ValueError) as e:
            return ResultadoConsulta(**base, estado="error", mensaje=f"No se pudo leer el catálogo: {e}")
        ofertas = []
        for i, f in enumerate(filas):
            if f.get("termino"):
                # Catálogo capturado por término: se respeta la búsqueda original.
                if normalizar(f["termino"]) != normalizar(termino):
                    continue
            elif not contiene_palabra(normalizar(f.get("nombre")), termino):
                continue
            try:
                precio = float(f["precio"]) if f.get("precio") not in (None, "") else None
            except ValueError:
                precio = None
            ofertas.append(
                Oferta(
                    id=f"{self.id}:{f.get('sku') or i}",
                    proveedor_id=self.id,
                    proveedor=self.nombre,
                    nombre=f.get("nombre", ""),
                    marca=f.get("marca") or None,
                    presentacion_texto=f.get("nombre", ""),
                    precio=precio,
                    moneda=f.get("moneda") or None,
                    url=f.get("url") or None,
                    sku=f.get("sku") or None,
                    fecha_consulta=f.get("fecha_consulta") or base["fecha_consulta"],
                    disponibilidad=f.get("disponibilidad") or "desconocida",
                    mecanismo=self.mecanismo,
                    fuente_consulta=f"{self.ruta.name} (origen: {f.get('fuente_consulta', 's.d.')})",
                )
            )
        if not ofertas:
            return ResultadoConsulta(**base, estado="sin_resultados", mensaje="Sin coincidencias en el catálogo.")
        return ResultadoConsulta(**base, estado="ok", ofertas=ofertas,
                                 mensaje=f"{len(ofertas)} productos del catálogo de referencia.")
