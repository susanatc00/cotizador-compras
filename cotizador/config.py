"""Configuración desde variables de entorno (.env)."""
import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    api_key: str
    modelo: str
    temperatura: Optional[float]
    base_url: Optional[str]


def hay_api_key() -> bool:
    load_dotenv()
    k = os.getenv("OPENAI_API_KEY", "").strip()
    return bool(k) and not k.startswith("sk-...")


def cargar_config() -> Config:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key or api_key.startswith("sk-..."):
        raise RuntimeError(
            "Falta OPENAI_API_KEY. Copia .env.example a .env y completa tu clave, "
            "o ejecuta con --sin-llm para usar las reglas de respaldo."
        )
    temp = os.getenv("OPENAI_TEMPERATURE", "0").strip()
    return Config(
        api_key=api_key,
        modelo=os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip(),
        temperatura=float(temp) if temp else None,
        base_url=os.getenv("OPENAI_BASE_URL", "").strip() or None,
    )
