"""test_classifier.py - Pruebas de classify_offer: Groq como proveedor
primario (con su propio backoff ante 429) y OpenRouter como respaldo
opcional cuando Groq falla del todo. Ninguna API real se llama: se mockea
classifier.groq_client.chat.completions.create,
classifier.openrouter_client.chat.completions.create y classifier.time.sleep
para poder comprobar el comportamiento sin esperar de verdad ni gastar
cuota."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import openai
import pytest

import classifier
from classifier import classify_offer, MAX_BACKOFF_SECONDS


def make_offer():
    return SimpleNamespace(
        title="Desarrollador/a Junior",
        company="Empresa Test",
        location="Granada",
        mode="Presencial",
        source="Adzuna",
        description="Prácticas de desarrollo web con Python y JavaScript.",
    )


def make_success_response(tier="A", justification="Encaja en DAW."):
    """Simula la forma de un ChatCompletion de la librería openai: lo único
    que lee classify_offer es response.choices[0].message.content."""
    message = SimpleNamespace(content=f'{{"tier": "{tier}", "justification": "{justification}"}}')
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice])


def make_http_response(status_code: int, retry_after: str | None = None):
    headers = {}
    if retry_after is not None:
        headers["retry-after"] = retry_after
    return SimpleNamespace(status_code=status_code, headers=headers, request=SimpleNamespace())


def make_rate_limit_error(retry_after: str | None = None) -> openai.RateLimitError:
    """Construye un RateLimitError tal y como lo lanza la librería openai
    ante un 429 de Groq/OpenRouter, con o sin el header HTTP estándar
    Retry-After (confirmado leyendo openai._base_client: la librería expone
    la respuesta HTTP cruda en error.response, con error.response.headers
    dando acceso a 'retry-after' en segundos como texto plano - a diferencia
    de Gemini, que mandaba un bloque RetryInfo anidado en el cuerpo JSON)."""
    response = make_http_response(429, retry_after=retry_after)
    return openai.RateLimitError("Rate limit exceeded", response=response, body={"error": {"message": "rate limit"}})


def make_bad_request_error() -> openai.BadRequestError:
    response = make_http_response(400)
    return openai.BadRequestError("Bad request", response=response, body={"error": {"message": "bad request"}})


class TestSuccessPath:
    def test_classifies_successfully_with_groq_without_retries(self):
        offer = make_offer()
        mock_create = MagicMock(return_value=make_success_response())

        with patch.object(classifier.groq_client.chat.completions, "create", mock_create), \
             patch.object(classifier.time, "sleep") as mock_sleep:
            tier, justification = classify_offer(offer)

        assert tier == "A"
        assert justification == "Encaja en DAW."
        mock_sleep.assert_not_called()
        mock_create.assert_called_once()
        assert mock_create.call_args.kwargs["model"] == classifier.GROQ_MODEL


class TestGroqRetryThenOpenRouterFallback:
    def test_retries_groq_on_429_using_retry_after_header(self):
        """Si el 429 trae el header Retry-After (p.ej. "2"), se espera
        exactamente eso en vez de backoff exponencial."""
        offer = make_offer()
        error = make_rate_limit_error(retry_after="2")
        mock_create = MagicMock(side_effect=[error, make_success_response()])

        with patch.object(classifier.groq_client.chat.completions, "create", mock_create), \
             patch.object(classifier.time, "sleep") as mock_sleep:
            tier, justification = classify_offer(offer)

        assert tier == "A"
        assert mock_sleep.call_count == 1
        waited = mock_sleep.call_args[0][0]
        assert waited == pytest.approx(2.0)

    def test_caps_retry_after_at_max_backoff(self):
        offer = make_offer()
        error = make_rate_limit_error(retry_after="600")
        mock_create = MagicMock(side_effect=[error, make_success_response()])

        with patch.object(classifier.groq_client.chat.completions, "create", mock_create), \
             patch.object(classifier.time, "sleep") as mock_sleep:
            classify_offer(offer)

        waited = mock_sleep.call_args[0][0]
        assert waited <= MAX_BACKOFF_SECONDS

    def test_falls_back_to_exponential_backoff_without_retry_after_header(self):
        offer = make_offer()
        error = make_rate_limit_error(retry_after=None)
        mock_create = MagicMock(side_effect=[error, error, error, make_success_response()])

        with patch.object(classifier.groq_client.chat.completions, "create", mock_create), \
             patch.object(classifier.time, "sleep") as mock_sleep, \
             patch.object(classifier.random, "uniform", return_value=0.0):
            tier, _ = classify_offer(offer)

        assert tier == "A"
        waits = [call.args[0] for call in mock_sleep.call_args_list]
        assert waits == sorted(waits)  # backoff creciente
        assert len(set(waits)) == len(waits)  # no repite el mismo valor
        assert all(w <= MAX_BACKOFF_SECONDS for w in waits)

    def test_openrouter_succeeds_after_groq_exhausts_retries(self):
        """Groq falla con 429 en todos los intentos; con OPENROUTER_API_KEY
        configurada (mockeada aquí vía classifier.openrouter_client), se
        prueba una vez con OpenRouter y su resultado es el final (no
        'error')."""
        offer = make_offer()
        error = make_rate_limit_error(retry_after="1")
        mock_groq_create = MagicMock(side_effect=[error, error, error, error])
        mock_openrouter_create = MagicMock(return_value=make_success_response(tier="B", justification="Candidata a Tier A."))
        mock_openrouter_client = MagicMock()
        mock_openrouter_client.chat.completions.create = mock_openrouter_create

        with patch.object(classifier.groq_client.chat.completions, "create", mock_groq_create), \
             patch.object(classifier, "openrouter_client", mock_openrouter_client), \
             patch.object(classifier.time, "sleep"):
            tier, justification = classify_offer(offer)

        assert tier == "B"
        assert justification == "Candidata a Tier A."
        mock_openrouter_create.assert_called_once()
        assert mock_openrouter_create.call_args.kwargs["model"] == classifier.OPENROUTER_MODEL

    def test_groq_fails_completely_without_openrouter_configured_returns_error(self):
        """Sin OPENROUTER_API_KEY (classifier.openrouter_client es None), si
        Groq agota los reintentos se devuelve 'error' sin intentar
        OpenRouter."""
        offer = make_offer()
        error = make_rate_limit_error(retry_after="1")
        mock_groq_create = MagicMock(side_effect=[error, error, error, error])

        with patch.object(classifier.groq_client.chat.completions, "create", mock_groq_create), \
             patch.object(classifier, "openrouter_client", None), \
             patch.object(classifier.time, "sleep"):
            tier, justification = classify_offer(offer)

        assert tier == "error"
        assert "Groq" in justification

    def test_groq_and_openrouter_both_fail_returns_error(self):
        offer = make_offer()
        groq_error = make_rate_limit_error(retry_after="1")
        mock_groq_create = MagicMock(side_effect=[groq_error, groq_error, groq_error, groq_error])
        openrouter_error = make_bad_request_error()
        mock_openrouter_create = MagicMock(side_effect=openrouter_error)
        mock_openrouter_client = MagicMock()
        mock_openrouter_client.chat.completions.create = mock_openrouter_create

        with patch.object(classifier.groq_client.chat.completions, "create", mock_groq_create), \
             patch.object(classifier, "openrouter_client", mock_openrouter_client), \
             patch.object(classifier.time, "sleep"):
            tier, justification = classify_offer(offer)

        assert tier == "error"
        mock_openrouter_create.assert_called_once()


class TestNonRetryableGroqErrors:
    def test_non_429_error_does_not_retry_and_tries_openrouter(self):
        """Un error no reintentable (400/401) de Groq no debe reintentarse
        (sin dormir), y debe pasar directo a intentar OpenRouter si está
        configurado."""
        offer = make_offer()
        error = make_bad_request_error()
        mock_groq_create = MagicMock(side_effect=error)
        mock_openrouter_create = MagicMock(return_value=make_success_response())
        mock_openrouter_client = MagicMock()
        mock_openrouter_client.chat.completions.create = mock_openrouter_create

        with patch.object(classifier.groq_client.chat.completions, "create", mock_groq_create), \
             patch.object(classifier, "openrouter_client", mock_openrouter_client), \
             patch.object(classifier.time, "sleep") as mock_sleep:
            tier, justification = classify_offer(offer)

        assert tier == "A"
        mock_sleep.assert_not_called()
        mock_groq_create.assert_called_once()
        mock_openrouter_create.assert_called_once()

    def test_non_429_error_without_openrouter_returns_error_without_retrying(self):
        offer = make_offer()
        error = make_bad_request_error()
        mock_groq_create = MagicMock(side_effect=error)

        with patch.object(classifier.groq_client.chat.completions, "create", mock_groq_create), \
             patch.object(classifier, "openrouter_client", None), \
             patch.object(classifier.time, "sleep") as mock_sleep:
            tier, justification = classify_offer(offer)

        assert tier == "error"
        mock_sleep.assert_not_called()
        mock_groq_create.assert_called_once()
