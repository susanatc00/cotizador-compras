"""Utilidades compartidas: registro con catálogos locales y LLM falsos (sin red ni API)."""
from pathlib import Path

import pytest
from langchain_core.runnables import RunnableLambda

from cotizador import reglas
from cotizador.proveedores import Registro
from cotizador.proveedores.catalogo import ProveedorCatalogo
from cotizador.schemas import ItemInterpretado, SolicitudInterpretada

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg(pid: str, nombre: str, envio=None) -> dict:
    return {
        "id": pid, "nombre": nombre, "alias": [pid.replace("_", " ")],
        "catalogo": str(FIXTURES / "catalogo_prueba.csv"),
        "envio": {"valor": envio, "nota": "prueba"},
        "condiciones_no_verificadas": [f"{nombre}: precio no verificado en {{ciudad}}."],
    }


def crear_registro(envio_a=None, envio_b=None) -> Registro:
    provs = {
        "tienda_a": ProveedorCatalogo(_cfg("tienda_a", "Tienda A", envio_a)),
        "tienda_b": ProveedorCatalogo(_cfg("tienda_b", "Tienda B", envio_b)),
    }
    return Registro(
        config={"ciudad_por_defecto": "Medellín"},
        proveedores=provs,
        no_soportados={"exito": {"alias": ["exito", "éxito"], "motivo": "usa endpoints internos"}},
    )


@pytest.fixture
def registro():
    return crear_registro()


def item(producto, termino, cantidad, unidad, **kw) -> ItemInterpretado:
    datos = dict(texto_original=f"{cantidad} {unidad} de {producto}", producto=producto, termino_busqueda=termino,
                 cantidad=cantidad, unidad=unidad, especificaciones=[], marca=None, marca_obligatoria=False,
                 presentacion_cantidad=None, presentacion_unidad=None, presentacion_obligatoria=False)
    datos.update(kw)
    return ItemInterpretado(**datos)


def interprete_fijo(*respuestas: SolicitudInterpretada):
    """LLM falso: devuelve las respuestas en orden (una por llamada) y registra las entradas."""
    llamadas = []

    def _f(x):
        llamadas.append(x["texto_usuario"])
        return respuestas[min(len(llamadas) - 1, len(respuestas) - 1)]

    r = RunnableLambda(_f)
    r.llamadas = llamadas  # type: ignore[attr-defined]
    return r


def solicitud(items, ciudad="Medellín", mencionados=None, cantidad=None) -> SolicitudInterpretada:
    return SolicitudInterpretada(items=items, ciudad=ciudad, proveedores_mencionados=mencionados or [],
                                 cantidad_proveedores=cantidad, observaciones=[])


@pytest.fixture
def juez():
    return reglas.juez_reglas()


@pytest.fixture
def redactor():
    return reglas.redactor_reglas()
