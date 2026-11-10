"""Modelos Pydantic: salida estructurada de los LLM, datos comerciales y estado del grafo.

Convención (igual que en `sistema_quejas`): cada nodo recibe el estado
`EstadoCotizacion` y devuelve un diccionario solo con los campos que actualiza.
Los campos con reducer (`operator.add`) acumulan en lugar de reemplazar.
"""
from __future__ import annotations

import operator
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# ---------------------------------------------------------------------------
# 1. Salida estructurada del intérprete (LLM)
# ---------------------------------------------------------------------------
# OpenAI Structured Outputs (strict) exige que todos los campos sean
# obligatorios: los opcionales se declaran como `X | None` sin valor por defecto.

UnidadSolicitud = Literal["kg", "g", "lb", "L", "mL", "und"]


class ItemInterpretado(BaseModel):
    model_config = ConfigDict(extra="forbid")

    texto_original: str = Field(description="Fragmento literal de la solicitud que describe este artículo.")
    producto: str = Field(description="Nombre genérico del producto en español, sin marca ni cantidad. Ej.: 'arroz blanco', 'aceite vegetal'.")
    termino_busqueda: str = Field(
        description="UNA sola palabra, en minúsculas y sin tildes, que identifique el producto en el buscador de un supermercado. Ej.: 'arroz', 'aceite', 'azucar', 'leche'."
    )
    cantidad: float | None = Field(description="Cantidad total pedida. null si el usuario no la dijo.")
    unidad: UnidadSolicitud | None = Field(
        description="Unidad de la cantidad: kg, g, lb, L, mL o und (unidades/paquetes). null si el usuario no la dijo."
    )
    especificaciones: list[str] = Field(
        description="Variedad, tipo o calidad exigida, en palabras cortas sin tildes. Ej.: ['blanco'], ['extra virgen'], ['deslactosada']. Lista vacía si no hay."
    )
    marca: str | None = Field(description="Marca mencionada por el usuario o null.")
    marca_obligatoria: bool = Field(
        description="true si el usuario exige esa marca ('solo Diana', 'marca Diana'); false si es preferencia ('ojalá Diana') o no hay marca."
    )
    presentacion_cantidad: float | None = Field(
        description="Tamaño de empaque pedido (ej. bolsas de 1 kg -> 1). null si no se indicó."
    )
    presentacion_unidad: UnidadSolicitud | None = Field(description="Unidad del tamaño de empaque o null.")
    presentacion_obligatoria: bool = Field(
        description="true si el usuario exige ese empaque ('en bolsas de 1 kg'); false si no se indicó o es indiferente."
    )


class SolicitudInterpretada(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ItemInterpretado]
    ciudad: str | None = Field(description="Ciudad o zona de compra mencionada, o null.")
    proveedores_mencionados: list[str] = Field(
        description="Nombres de tiendas que el usuario pidió consultar, tal como los escribió. Lista vacía si no nombró ninguna."
    )
    cantidad_proveedores: int | None = Field(
        description="Número de proveedores pedido ('consulta dos supermercados' -> 2) o null."
    )
    observaciones: list[str] = Field(
        description="Ambigüedades o datos dudosos detectados en la solicitud. Lista vacía si no hay."
    )


# ---------------------------------------------------------------------------
# 2. Juez de equivalencia (LLM)
# ---------------------------------------------------------------------------
CategoriaSemantica = Literal["equivalente", "sustituto", "no_corresponde"]


class VeredictoOferta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Identificador de la oferta, copiado tal cual.")
    categoria: CategoriaSemantica
    motivo: str = Field(description="Justificación breve (máx. 15 palabras).")


class VeredictosItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    veredictos: list[VeredictoOferta]


# ---------------------------------------------------------------------------
# 3. Datos comerciales
# ---------------------------------------------------------------------------
Dimension = Literal["masa", "volumen", "unidad", "longitud"]
Disponibilidad = Literal["disponible", "agotado", "desconocida"]


class Presentacion(BaseModel):
    """Contenido de un empaque, normalizado a unidad base (kg, L, und, m)."""

    cantidad_base: float | None = None  # contenido total del empaque en unidad base
    dimension: Dimension | None = None
    unidades_por_empaque: int | None = None  # p. ej. paca x25, six-pack, 12 rollos
    texto: str = ""  # cómo se leyó, p. ej. "25 x 500 g = 12.5 kg"
    inferida: bool = False  # True si se aplicó una heurística (ver unidades.py)


class Oferta(BaseModel):
    """Un producto publicado por un proveedor, con su trazabilidad."""

    id: str
    proveedor_id: str
    proveedor: str
    nombre: str
    marca: str | None = None
    presentacion_texto: str = ""  # texto de donde se leyó la presentación (nombre)
    precio: float | None = None
    moneda: str | None = None
    url: str | None = None
    sku: str | None = None
    fecha_consulta: str  # ISO 8601
    disponibilidad: Disponibilidad = "desconocida"
    mecanismo: str  # "jsonld_publico" | "catalogo_referencia"
    fuente_consulta: str  # URL de la página consultada o ruta del catálogo


EstadoConsulta = Literal["ok", "sin_resultados", "bloqueado_robots", "error"]


class ResultadoConsulta(BaseModel):
    """Resultado de consultar UN artículo en UN proveedor."""

    proveedor_id: str
    proveedor: str
    item_idx: int
    termino: str
    estado: EstadoConsulta
    url_consulta: str | None = None
    fecha_consulta: str
    mensaje: str = ""
    ofertas: list[Oferta] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4. Solicitud normalizada (lo que usa el resto del grafo)
# ---------------------------------------------------------------------------
class ItemSolicitado(BaseModel):
    idx: int
    texto_original: str
    producto: str
    termino_busqueda: str
    cantidad: float  # en unidad base
    dimension: Dimension
    unidad_base: str  # kg | L | und
    cantidad_texto: str  # p. ej. "5 kg"
    especificaciones: list[str] = Field(default_factory=list)
    marca: str | None = None
    marca_obligatoria: bool = False
    presentacion_base: float | None = None  # tamaño de empaque exigido, en unidad base
    presentacion_obligatoria: bool = False


class Solicitud(BaseModel):
    items: list[ItemSolicitado]
    ciudad: str
    proveedores_ids: list[str]


# ---------------------------------------------------------------------------
# 5. Comparación y presupuesto
# ---------------------------------------------------------------------------
Clasificacion = Literal["equivalente", "sustituto", "no_comparable", "descartado"]


class OfertaEvaluada(BaseModel):
    item_idx: int
    oferta: Oferta
    clasificacion: Clasificacion
    elegible: bool  # puede entrar al presupuesto
    motivos: list[str] = Field(default_factory=list)
    presentacion: Presentacion = Field(default_factory=Presentacion)
    precio_unitario: float | None = None  # precio por unidad base (COP/kg, COP/L, COP/und)
    empaques: int | None = None  # empaques necesarios para cubrir la cantidad
    costo: float | None = None  # empaques * precio
    excedente: float | None = None  # cantidad comprada de más, en unidad base


class LineaPresupuesto(BaseModel):
    item_idx: int
    producto: str
    cantidad_texto: str
    evaluada: OfertaEvaluada | None = None  # None = sin cotización en este proveedor
    nota: str = ""


class CostoAdicional(BaseModel):
    concepto: str
    valor: float | None  # None = desconocido (NUNCA se asume cero)
    fuente: str = ""
    nota: str = ""


class PresupuestoOpcion(BaseModel):
    nombre: str  # "Olímpica", "Jumbo", "Compra combinada"
    tipo: Literal["proveedor_unico", "combinada"]
    proveedores: list[str]
    lineas: list[LineaPresupuesto]
    cobertura: Literal["completa", "completa_con_sustitutos", "parcial", "nula"]
    items_sin_cotizacion: list[str] = Field(default_factory=list)
    subtotal_productos: float = 0.0
    costos_adicionales: list[CostoAdicional] = Field(default_factory=list)
    total_conocido: float = 0.0  # subtotal + costos adicionales conocidos
    total_final_confirmado: float | None = None  # solo si TODO está confirmado
    pendientes: list[str] = Field(default_factory=list)  # por qué no hay total confirmado
    viable: bool = True
    nota: str = ""


class Recomendacion(BaseModel):
    opcion: str | None
    criterio: str
    texto: str = ""


# ---------------------------------------------------------------------------
# 6. Estado del grafo
# ---------------------------------------------------------------------------
class EstadoCotizacion(BaseModel):
    texto_usuario: str = ""
    modo_interactivo: bool = False
    rondas_aclaracion: int = 0
    interpretacion: Optional[SolicitudInterpretada] = None
    faltantes: list[str] = Field(default_factory=list)
    solicitud: Optional[Solicitud] = None
    suposiciones: Annotated[list[str], operator.add] = Field(default_factory=list)
    resultados_consulta: Annotated[list[ResultadoConsulta], operator.add] = Field(default_factory=list)
    evaluadas: list[OfertaEvaluada] = Field(default_factory=list)
    opciones: list[PresupuestoOpcion] = Field(default_factory=list)
    recomendacion: Optional[Recomendacion] = None
    condiciones_no_verificadas: list[str] = Field(default_factory=list)
    reporte_md: str = ""
    archivos_salida: list[str] = Field(default_factory=list)
    historial: Annotated[list[str], operator.add] = Field(default_factory=list)
