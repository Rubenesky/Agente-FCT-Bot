"""test_main_search_sources.py - Pruebas de las fuentes de búsqueda Jooble y
EURES (main.py).

Jooble: agregador con cobertura España, requiere API key opcional
(JOOBLE_API_KEY). Esquema de petición/respuesta confirmado en la
documentación oficial de Jooble (help.jooble.org/en/support/solutions/
articles/60001448238-rest-api-documentation): POST a
https://es.jooble.org/api/{key} con body {"keywords", "location"}, respuesta
{"totalCount": int, "jobs": [{"id","title","location","snippet","salary",
"source","type","link","company","updated"}]}.

EURES: API pública del portal de empleo de la UE, sin key. Esquema
confirmado en vivo (01/11/2026) contra el endpoint real (POST
https://europa.eu/eures/api/jv-searchengine/public/jv-search/search):
respuesta {"numberRecords": int, "jvs": [{"id", "title", "description",
"locationMap": {"ES": ["ES614", ...]}, ...}]}. El filtro locationCodes solo
funciona de forma fiable a nivel de comunidad autónoma (NUTS2, "es61" =
Andalucía) - filtrar directamente por NUTS3 (Granada/Málaga) devuelve
siempre 0 resultados pese a que esos códigos sí aparecen en locationMap, de
ahí que search_eures pida toda Andalucía y filtre a mano por ciudad.

No se llama a ninguna API real: se mockea requests.post/requests.get.
"""
from unittest.mock import MagicMock, patch

import main


JOOBLE_RESPONSE_OK = {
    "totalCount": 2,
    "jobs": [
        {
            "id": 1234567890,
            "title": "Becario/a Desarrollo DAW",
            "location": "Granada",
            "snippet": "Buscamos becario/a para prácticas de desarrollo web...",
            "salary": "",
            "source": "jooble",
            "type": "Full-time",
            "link": "https://es.jooble.org/jdp/12345",
            "company": "Empresa Ejemplo",
            "updated": "2026-09-01T12:00:00.0000000",
        },
        {
            "id": 987654321,
            "title": "Prácticas remotas programación",
            "location": "Málaga",
            "snippet": "Prácticas en modalidad remota para estudiantes...",
            "salary": "",
            "source": "jooble",
            "type": "Internship",
            "link": "https://es.jooble.org/jdp/98765",
            "company": "Otra Empresa",
            "updated": "2026-09-02T09:30:00.0000000",
        },
    ],
}

EURES_RESPONSE_OK = {
    "numberRecords": 2,
    "jvs": [
        {
            "id": "NzQ1ODA3MiAxMDI",
            "title": "DESARROLLADOR/A .NET (IKERLAN)",
            "description": "Desarrollo de nuevas funcionalidades en entorno .NET...",
            "locationMap": {"ES": ["ES614"]},
        },
        {
            "id": "NzQ2MDI3MCAxMDI",
            "title": "DISEÑADOR WEB EN REMOTO",
            "description": "Puesto 100% en remoto, teletrabajo completo...",
            "locationMap": {"ES": ["ES617"]},
        },
        {
            "id": "OTHERPROVINCE123",
            "title": "COMERCIAL (CÓRDOBA)",
            "description": "Oferta en Córdoba, fuera de Granada/Málaga...",
            "locationMap": {"ES": ["ES613"]},
        },
    ],
}


def make_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


class TestSearchJooble:
    def test_parses_offers_from_response(self):
        with patch.object(main, "JOOBLE_API_KEY", "fake-key"), \
             patch.object(main.requests, "post", return_value=make_response(200, JOOBLE_RESPONSE_OK)) as post_mock:
            offers = main.search_jooble("DAW", "Granada")

        post_mock.assert_called_once()
        args, kwargs = post_mock.call_args
        assert args[0] == "https://es.jooble.org/api/fake-key"
        assert kwargs["json"] == {"keywords": "DAW", "location": "Granada"}

        assert len(offers) == 2
        first = offers[0]
        assert isinstance(first, main.JobOffer)
        assert first.title == "Becario/a Desarrollo DAW"
        assert first.company == "Empresa Ejemplo"
        assert first.url == "https://es.jooble.org/jdp/12345"
        assert first.source == "Jooble"
        assert "prácticas" in first.description.lower()
        assert first.location == "Granada"

    def test_no_api_key_returns_empty_without_http_call(self):
        with patch.object(main, "JOOBLE_API_KEY", None), \
             patch.object(main.requests, "post") as post_mock:
            offers = main.search_jooble("DAW", "Granada")

        assert offers == []
        post_mock.assert_not_called()

    def test_http_error_status_returns_empty_list(self):
        with patch.object(main, "JOOBLE_API_KEY", "fake-key"), \
             patch.object(main.requests, "post", return_value=make_response(500, {})):
            offers = main.search_jooble("DAW", "Granada")

        assert offers == []

    def test_timeout_returns_empty_list(self):
        with patch.object(main, "JOOBLE_API_KEY", "fake-key"), \
             patch.object(main.requests, "post", side_effect=main.requests.exceptions.Timeout("timeout")):
            offers = main.search_jooble("DAW", "Granada")

        assert offers == []


class TestSearchEures:
    def test_parses_offers_and_filters_by_target_city(self):
        with patch.object(main.requests, "post", return_value=make_response(200, EURES_RESPONSE_OK)) as post_mock:
            offers = main.search_eures("desarrollador")

        post_mock.assert_called_once()
        args, kwargs = post_mock.call_args
        assert args[0] == "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
        assert kwargs["json"]["locationCodes"] == [main.EURES_REGION_CODE]
        assert kwargs["json"]["keywords"] == [{"keyword": "desarrollador", "specificSearchCode": "TITLE"}]

        # Solo 2 de las 3 ofertas del fixture son de Granada/Málaga (ES614/
        # ES617); la de Córdoba (ES613) se descarta.
        assert len(offers) == 2
        assert all(isinstance(o, main.JobOffer) for o in offers)
        assert {o.location for o in offers} == {"Granada", "Málaga"}
        assert all(o.source == "EURES" for o in offers)

        granada_offer = next(o for o in offers if o.location == "Granada")
        assert granada_offer.title == "DESARROLLADOR/A .NET (IKERLAN)"
        assert granada_offer.url == "https://europa.eu/eures/portal/jv-se/jv-details/NzQ1ODA3MiAxMDI?lang=es"
        assert granada_offer.company == "Empresa no especificada"
        assert granada_offer.mode == "Presencial"

        malaga_offer = next(o for o in offers if o.location == "Málaga")
        assert malaga_offer.mode == "Remoto"  # "teletrabajo" en la descripción

    def test_http_error_status_returns_empty_list(self):
        with patch.object(main.requests, "post", return_value=make_response(500, {})):
            offers = main.search_eures("desarrollador")

        assert offers == []

    def test_timeout_returns_empty_list(self):
        with patch.object(main.requests, "post", side_effect=main.requests.exceptions.Timeout("timeout")):
            offers = main.search_eures("desarrollador")

        assert offers == []


class TestSearchNodeIntegratesNewSources:
    """Confirma que Jooble se llama una sola vez por ciudad (no por cada
    combinación ciudad/keyword, para no agotar el límite de solicitudes de
    la cuenta gratuita) y que EURES se llama una sola vez por keyword, fuera
    del bucle de ciudades (ver nota junto a search_eures)."""

    def make_state(self):
        return {"offers": [], "seen_companies": set(), "finished": False}

    def test_jooble_called_once_per_city_not_multiplied_by_keywords(self):
        with patch.object(main, "search_adzuna", return_value=[]), \
             patch.object(main, "search_tecnoempleo_rss", return_value=[]), \
             patch.object(main, "search_teamtailor", return_value=[]), \
             patch.object(main, "search_jooble", return_value=[]) as jooble_mock, \
             patch.object(main, "search_eures", return_value=[]), \
             patch.object(main.db, "is_seen", return_value=False), \
             patch.object(main.time, "sleep"):
            main.search_node(self.make_state())

        assert jooble_mock.call_count == len(main.CITIES)

    def test_eures_called_once_per_keyword_not_multiplied_by_cities(self):
        with patch.object(main, "search_adzuna", return_value=[]), \
             patch.object(main, "search_tecnoempleo_rss", return_value=[]), \
             patch.object(main, "search_teamtailor", return_value=[]), \
             patch.object(main, "search_jooble", return_value=[]), \
             patch.object(main, "search_eures", return_value=[]) as eures_mock, \
             patch.object(main.db, "is_seen", return_value=False), \
             patch.object(main.time, "sleep"):
            main.search_node(self.make_state())

        assert eures_mock.call_count == len(main.KEYWORDS)
        assert eures_mock.call_count != len(main.CITIES) * len(main.KEYWORDS)

    def test_eures_offers_are_added_to_all_found(self):
        offer = main.JobOffer(
            title="Desarrollador/a Backend",
            location="Granada",
            mode="Presencial",
            company="Empresa no especificada",
            url="https://europa.eu/eures/portal/jv-se/jv-details/abc123?lang=es",
            source="EURES",
        )
        with patch.object(main, "search_adzuna", return_value=[]), \
             patch.object(main, "search_tecnoempleo_rss", return_value=[]), \
             patch.object(main, "search_teamtailor", return_value=[]), \
             patch.object(main, "search_jooble", return_value=[]), \
             patch.object(main, "search_eures", return_value=[offer]), \
             patch.object(main.db, "is_seen", return_value=False), \
             patch.object(main.time, "sleep"):
            state = main.search_node(self.make_state())

        assert any(o.url == offer.url for o in state["offers"])

    def test_logs_jooble_disabled_warning_only_once_when_no_api_key(self, caplog):
        import logging
        with patch.object(main, "JOOBLE_API_KEY", None), \
             patch.object(main, "search_adzuna", return_value=[]), \
             patch.object(main, "search_tecnoempleo_rss", return_value=[]), \
             patch.object(main, "search_teamtailor", return_value=[]), \
             patch.object(main, "search_eures", return_value=[]), \
             patch.object(main.db, "is_seen", return_value=False), \
             patch.object(main.time, "sleep"), \
             caplog.at_level(logging.WARNING, logger="main"):
            main.search_node(self.make_state())

        warnings = [r for r in caplog.records if "JOOBLE_API_KEY" in r.message]
        assert len(warnings) == 1
