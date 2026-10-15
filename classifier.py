"""classifier.py - Clasificación Tier A / Tier B / descarte.

Proveedor primario: Groq (nivel gratuito, API compatible con OpenAI). Si Groq
agota sus reintentos ante un 429 o falla con un error no recuperable, se
intenta una vez con OpenRouter como respaldo (solo si OPENROUTER_API_KEY está
configurada). Ya no se usa Gemini para nada (ver historial de git si se
necesita la implementación anterior)."""
import logging
import random
import time
from typing import Literal

from openai import OpenAI, RateLimitError
from pydantic import BaseModel, Field, ValidationError

from config import GROQ_API_KEY, OPENROUTER_API_KEY, TIER_A_COMPANIES

logger = logging.getLogger(__name__)

# max_retries=0 en ambos clientes: el reintento ante 429 lo controla por
# completo el bucle de más abajo (con su propio logging y backoff), en vez
# de dejar que la librería openai reintente también por su cuenta por
# debajo (que haría un doble reintento silencioso, sin logs, con su propio
# calendario de espera superpuesto al nuestro).
groq_client = OpenAI(
    api_key=GROQ_API_KEY,
    base_url="https://api.groq.com/openai/v1",
    max_retries=0,
)

openrouter_client = (
    OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
        max_retries=0,
    )
    if OPENROUTER_API_KEY
    else None
)
if openrouter_client is None:
    logger.warning(
        "Clasificador: OPENROUTER_API_KEY no configurada - sin respaldo si "
        "Groq falla, el sistema depende solo de Groq."
    )

# Verificados en vivo el 15/10/2026 contra las APIs reales antes de fijarlos
# (los catálogos de modelos gratuitos de Groq/OpenRouter cambian con
# frecuencia - no asumir que un modelo mencionado en documentación externa
# sigue existiendo/disponible sin probarlo primero).
GROQ_MODEL = "openai/gpt-oss-120b"
OPENROUTER_MODEL = "nvidia/nemotron-3-super-120b-a12b:free"


class OfferClassification(BaseModel):
    tier: Literal["A", "B", "descarte"]
    justification: str = Field(description="Justificación breve (1-2 frases) de la clasificación.")


SYSTEM_PROMPT = f"""Eres un clasificador de ofertas de prácticas FCT (Formación en Centros de \
Trabajo) para un estudiante de CFGS de Desarrollo de Aplicaciones Web (DAW).

Clasifica cada oferta en exactamente una de estas categorías:

TIER A: la empresa está en esta lista de empresas objetivo ya mapeadas:
{", ".join(TIER_A_COMPANIES)}
Y además la ubicación cumple la regla de ubicación (ver abajo) Y el contenido de la oferta encaja \
en el currículum de DAW (desarrollo web/software).

TIER B: la empresa NO está en la lista de arriba, pero la ubicación cumple la regla Y el contenido \
encaja claramente en el currículum de DAW. Es candidata a promoción a Tier A en el futuro.

DESCARTE: aplica si ocurre cualquiera de estas condiciones:
- El contenido no encaja en el currículum de DAW (desarrollo web/software), aunque esté etiquetada \
como "prácticas" (p.ej. becas de otro perfil: administración, marketing, ventas, etc.)
- Es presencial fuera de Granada o Málaga sin ser remota
- Es un empleo junior a jornada completa disfrazado de prácticas (no es un puesto de prácticas real)

REGLA DE UBICACIÓN: la oferta es válida si es presencial o híbrida en Granada o Málaga, O si es \
remota (independientemente de dónde esté la empresa). Una oferta presencial fuera de Granada/Málaga \
que no sea remota se descarta siempre.

Devuelve el tier y una justificación breve (1-2 frases) de tu decisión.

Responde ÚNICAMENTE con un objeto JSON, sin texto adicional antes ni después, ni bloques de código, \
con exactamente estas dos claves:
{{"tier": "A" | "B" | "descarte", "justification": "<justificación breve en 1-2 frases>"}}"""


MAX_RETRIES = 3  # solo para 429 (rate limit) - otros errores no se reintentan
BASE_BACKOFF_SECONDS = 5  # base del backoff exponencial cuando la API no da Retry-After
MAX_BACKOFF_SECONDS = 60  # tope superior de espera, venga de Retry-After o del backoff exponencial


def _extract_retry_after_seconds(error: RateLimitError) -> float | None:
    """Busca el header HTTP estándar `Retry-After` que Groq (y, en general,
    cualquier API compatible con OpenAI) puede devolver en la respuesta 429.

    A diferencia de Gemini (que mandaba un bloque RetryInfo propio dentro del
    cuerpo JSON del error), la librería `openai` expone la respuesta HTTP
    cruda en `error.response` para cualquier APIStatusError (RateLimitError
    incluida), con `error.response.headers` dando acceso directo a
    `retry-after` (confirmado leyendo `_base_client.py` de la librería
    `openai` instalada: su propio mecanismo interno de reintento, que aquí
    desactivamos con max_retries=0, parsea ese mismo header con
    `_parse_retry_after_header`). El valor es siempre segundos en texto
    plano (no hay bloque anidado que parsear como en Gemini).

    Devuelve None si el error no trae ese header o no se puede parsear como
    float.
    """
    response = getattr(error, "response", None)
    headers = getattr(response, "headers", None)
    if headers is None:
        return None
    value = headers.get("retry-after")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _build_messages(offer) -> list[dict]:
    offer_text = (
        f"Título: {offer.title}\n"
        f"Empresa: {offer.company}\n"
        f"Ubicación: {offer.location}\n"
        f"Modalidad: {offer.mode}\n"
        f"Fuente: {offer.source}\n"
        f"Descripción: {offer.description[:1500]}"
    )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": offer_text},
    ]


def _attempt(client: OpenAI, model: str, messages: list[dict]) -> OfferClassification:
    """Un único intento de clasificación contra `client`. Propaga cualquier
    excepción (de red, de la API, o de validación del JSON devuelto) sin
    reintentar - el reintento ante 429 lo gestiona _classify_with_groq."""
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
    )
    content = response.choices[0].message.content
    return OfferClassification.model_validate_json(content)


def _classify_with_groq(messages: list[dict]) -> OfferClassification:
    """Intenta clasificar con Groq, reintentando ante 429 (límite de
    peticiones) con backoff. Lanza la excepción (sin reintentar más) en
    cuanto el error no es un 429, o en cuanto se agotan los MAX_RETRIES
    reintentos - quien llama decide entonces si recurre a OpenRouter."""
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES + 1):
        try:
            return _attempt(groq_client, GROQ_MODEL, messages)
        except RateLimitError as e:
            last_error = e
            if attempt >= MAX_RETRIES:
                break
            retry_after = _extract_retry_after_seconds(e)
            if retry_after is not None:
                wait = min(retry_after, MAX_BACKOFF_SECONDS)
                source = "Retry-After de la API"
            else:
                # Backoff exponencial con jitter (base 5s: 5, 10, 20... +
                # hasta BASE_BACKOFF_SECONDS de jitter aleatorio), para no
                # sincronizar reintentos con otros procesos que choquen con
                # el mismo límite de cuota.
                wait = min(
                    MAX_BACKOFF_SECONDS,
                    BASE_BACKOFF_SECONDS * (2 ** attempt) + random.uniform(0, BASE_BACKOFF_SECONDS),
                )
                source = "backoff exponencial con jitter"
            logger.warning(
                f"Clasificador: límite de peticiones de Groq (429), "
                f"reintento {attempt + 1}/{MAX_RETRIES} en {wait:.1f}s ({source})"
            )
            time.sleep(wait)
    raise last_error


def classify_offer(offer) -> tuple[str, str]:
    """Clasifica una oferta, primero con Groq y, si falla del todo, con
    OpenRouter como respaldo (solo si OPENROUTER_API_KEY está configurada).

    Devuelve (tier, justificación). tier es 'A', 'B', 'descarte', o 'error' si
    la clasificación automática falló con ambos proveedores (o solo con Groq,
    si no hay respaldo configurado). Un 'error' NUNCA se trata como descarte:
    la oferta se manda igualmente a revisión humana, con el motivo del fallo
    como justificación.
    """
    messages = _build_messages(offer)

    try:
        result = _classify_with_groq(messages)
        return result.tier, result.justification
    except (RateLimitError, ValidationError, Exception) as e:
        logger.error(f"Clasificador: Groq falló ({e})")
        groq_error = e

    if openrouter_client is None:
        return "error", f"No se pudo clasificar: Groq falló ({groq_error}) y no hay OPENROUTER_API_KEY configurada"

    try:
        result = _attempt(openrouter_client, OPENROUTER_MODEL, messages)
        return result.tier, result.justification
    except Exception as e:
        logger.error(f"Clasificador: respaldo de OpenRouter también falló ({e})")
        return "error", f"No se pudo clasificar: Groq falló ({groq_error}) y OpenRouter también falló ({e})"
