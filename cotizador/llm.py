"""Construcción de los componentes LLM (OpenAI vía LangChain).

Tres cadenas, cada una `prompt | modelo`:
- intérprete : texto -> SolicitudInterpretada   (Structured Outputs, json_schema estricto)
- juez       : artículo + ofertas -> VeredictosItem (Structured Outputs)
- redactor   : resumen JSON -> texto de la recomendación

Cada una tiene un equivalente por reglas en `reglas.py` (modo --sin-llm y pruebas).
"""
from __future__ import annotations

from typing import Optional

from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI

from cotizador.config import cargar_config
from cotizador.prompts import PLANTILLA_INTERPRETE, PLANTILLA_JUEZ, PLANTILLA_REDACTOR
from cotizador.schemas import SolicitudInterpretada, VeredictosItem


def crear_modelo() -> ChatOpenAI:
    cfg = cargar_config()
    kwargs = dict(model=cfg.modelo, api_key=cfg.api_key, base_url=cfg.base_url, timeout=90, max_retries=2)
    # Los modelos de razonamiento (gpt-5*, o*) solo aceptan la temperatura por defecto.
    if cfg.temperatura is not None and not cfg.modelo.startswith(("gpt-5", "o1", "o3", "o4")):
        kwargs["temperature"] = cfg.temperatura
    return ChatOpenAI(**kwargs)


def construir_interprete(llm: Optional[BaseChatModel] = None) -> Runnable:
    llm = llm or crear_modelo()
    return PLANTILLA_INTERPRETE | llm.with_structured_output(
        SolicitudInterpretada, method="json_schema", strict=True
    )


def construir_juez(llm: Optional[BaseChatModel] = None) -> Runnable:
    llm = llm or crear_modelo()
    return PLANTILLA_JUEZ | llm.with_structured_output(VeredictosItem, method="json_schema", strict=True)


def construir_redactor(llm: Optional[BaseChatModel] = None) -> Runnable:
    llm = llm or crear_modelo()
    return PLANTILLA_REDACTOR | llm | StrOutputParser()
