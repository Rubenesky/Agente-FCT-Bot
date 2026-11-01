"""test_main_search_sources_legacy.py - Pruebas de caracterización de las
fuentes de búsqueda ORIGINALES de agente-fct (main.py): search_adzuna,
search_tecnoempleo_rss y search_teamtailor. Estas funciones ya están en
producción desde hace tiempo (no son código nuevo); las pruebas de este
fichero documentan el comportamiento real observado leyendo main.py, no un
comportamiento asumido o "deseable".

Jooble (añadida más recientemente) ya tiene su propia cobertura en
test_main_search_sources.py - no se duplica aquí.

Adzuna: GET https://api.adzuna.com/v1/api/jobs/es/search/1 con params
app_id/app_key/what/where/results_per_page/content-type. Respuesta
{"results": [{"title", "description", "redirect_url", "location":
{"display_name"}, "company": {"display_name"}}, ...]}. El modo (Presencial/
Remoto/Híbrida) se infiere de palabras clave en `description` (no en el
título). location y company caen a valores por defecto si el campo no es un
dict (aunque en la práctica Adzuna siempre los devuelve como dict).

Tecnoempleo: RSS parseado con feedparser (no hay comprobación de código HTTP
- si el feed viene vacío o feedparser no puede parsearlo, feed.entries
está vacío y el bucle simplemente no añade nada => devuelve []). El modo se
infiere de palabras clave en el TÍTULO (no hay descripción en el RSS). La
ubicación se extrae buscando una lista fija de ciudades como substring del
título (si no aparece ninguna, se usa la `location` pasada como argumento).
La empresa se extrae partiendo el título por " en " y luego por " - ".

Teamtailor: GET https://<subdomain>/jobs.json (JSON Feed con extensión
_jobposting / schema.org JobPosting). location viene de
item._jobposting.jobLocation[0].address.addressLocality (con fallback a
"No especificada" si no hay jobLocation o si addressLocality es falsy). mode
es "Remoto" solo si jobLocationType == "TELECOMMUTE", si no
"No especificado" (nunca "Presencial" - a diferencia de Adzuna/Tecnoempleo).
description limpia las etiquetas HTML de content_html con una regex y se
trunca a 1500 caracteres.

No se llama a ninguna API real: se mockea requests.get / feedparser.parse.
"""
from unittest.mock import MagicMock, patch

import main


def make_response(status_code=200, json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    return resp


# ============================================
# Adzuna
# ============================================

ADZUNA_RESPONSE_OK = {
    "results": [
        {
            "title": "Becario/a Desarrollo DAW",
            "description": "Buscamos becario/a para prácticas presenciales de "
                            "desarrollo web en nuestras oficinas de Granada.",
            "redirect_url": "https://www.adzuna.es/land/ad/1111111",
            "location": {"display_name": "Granada, Andalucía"},
            "company": {"display_name": "Empresa Ejemplo"},
        },
        {
            "title": "Prácticas Backend Python",
            "description": "Puesto 100% remoto para estudiantes de último curso.",
            "redirect_url": "https://www.adzuna.es/land/ad/2222222",
            "location": {"display_name": "Málaga, Andalucía"},
            "company": {"display_name": "Otra Empresa"},
        },
    ]
}


class TestSearchAdzuna:
    def test_parses_offers_from_response(self):
        with patch.object(main.requests, "get", return_value=make_response(200, ADZUNA_RESPONSE_OK)) as get_mock:
            offers = main.search_adzuna("DAW", "Granada")

        get_mock.assert_called_once()
        args, kwargs = get_mock.call_args
        assert args[0] == "https://api.adzuna.com/v1/api/jobs/es/search/1"
        assert kwargs["params"]["what"] == "DAW"
        assert kwargs["params"]["where"] == "Granada"
        assert kwargs["timeout"] == 15

        assert len(offers) == 2
        first = offers[0]
        assert isinstance(first, main.JobOffer)
        assert first.title == "Becario/a Desarrollo DAW"
        assert first.company == "Empresa Ejemplo"
        assert first.location == "Granada, Andalucía"
        assert first.url == "https://www.adzuna.es/land/ad/1111111"
        assert first.source == "Adzuna"
        assert first.mode == "Presencial"
        assert first.description == ADZUNA_RESPONSE_OK["results"][0]["description"][:500]

        second = offers[1]
        assert second.mode == "Remoto"
        assert second.company == "Otra Empresa"

    def test_mode_hibrida_inferred_from_description(self):
        data = {
            "results": [
                {
                    "title": "Prácticas Fullstack",
                    "description": "Modalidad híbrida: 2 días en oficina y 3 en teletrabajo.",
                    "redirect_url": "https://www.adzuna.es/land/ad/333",
                    "location": {"display_name": "Sevilla"},
                    "company": {"display_name": "Empresa Hibrida"},
                }
            ]
        }
        with patch.object(main.requests, "get", return_value=make_response(200, data)):
            offers = main.search_adzuna("Fullstack", "Sevilla")

        assert len(offers) == 1
        assert offers[0].mode == "Híbrida"

    def test_missing_optional_fields_fall_back_to_defaults(self):
        data = {
            "results": [
                {
                    # sin title, sin description, sin redirect_url, sin location/company dicts
                }
            ]
        }
        with patch.object(main.requests, "get", return_value=make_response(200, data)):
            offers = main.search_adzuna("DAW", "Granada")

        assert len(offers) == 1
        offer = offers[0]
        assert offer.title == "Sin título"
        assert offer.url == "#"
        assert offer.company == "Empresa desconocida"
        assert offer.location == "Granada"
        assert offer.mode == "Presencial"

    def test_http_error_status_returns_empty_list(self):
        with patch.object(main.requests, "get", return_value=make_response(500, {})):
            offers = main.search_adzuna("DAW", "Granada")

        assert offers == []

    def test_timeout_returns_empty_list(self):
        with patch.object(main.requests, "get", side_effect=main.requests.exceptions.Timeout("timeout")):
            offers = main.search_adzuna("DAW", "Granada")

        assert offers == []

    def test_generic_exception_returns_empty_list(self):
        with patch.object(main.requests, "get", side_effect=ValueError("respuesta corrupta")):
            offers = main.search_adzuna("DAW", "Granada")

        assert offers == []


# ============================================
# Tecnoempleo RSS
# ============================================

class FakeEntry:
    """Simula un entry de feedparser: acceso por atributo a title/link."""
    def __init__(self, title, link):
        self.title = title
        self.link = link


class FakeFeed:
    """Simula el objeto devuelto por feedparser.parse: solo se usa
    feed.entries en el código real."""
    def __init__(self, entries):
        self.entries = entries


class TestSearchTecnoempleoRSS:
    def test_parses_offers_from_feed(self):
        entries = [
            FakeEntry(
                title="Becario/a Desarrollo Web en Grupo Software - Granada",
                link="https://www.tecnoempleo.com/ofertas/1",
            ),
            FakeEntry(
                title="Prácticas Backend remoto en TechCorp - Madrid",
                link="https://www.tecnoempleo.com/ofertas/2",
            ),
        ]
        with patch.object(main.feedparser, "parse", return_value=FakeFeed(entries)) as parse_mock:
            offers = main.search_tecnoempleo_rss("DAW", "España")

        parse_mock.assert_called_once_with("https://www.tecnoempleo.com/rss/empleo/?q=DAW")

        assert len(offers) == 2
        first = offers[0]
        assert isinstance(first, main.JobOffer)
        assert first.title == "Becario/a Desarrollo Web en Grupo Software - Granada"
        assert first.url == "https://www.tecnoempleo.com/ofertas/1"
        assert first.company == "Grupo Software"
        assert first.location == "Granada"
        assert first.mode == "Presencial"
        assert first.source == "Tecnoempleo"
        # description == title (el RSS no trae descripción propia)
        assert first.description == first.title

        second = offers[1]
        assert second.company == "TechCorp"
        assert second.location == "Madrid"
        assert second.mode == "Remoto"

    def test_keyword_with_spaces_is_url_encoded_with_plus(self):
        with patch.object(main.feedparser, "parse", return_value=FakeFeed([])) as parse_mock:
            main.search_tecnoempleo_rss("desarrollo web", "España")

        parse_mock.assert_called_once_with(
            "https://www.tecnoempleo.com/rss/empleo/?q=desarrollo+web"
        )

    def test_mode_hibrida_inferred_from_title(self):
        entries = [
            FakeEntry(
                title="Prácticas QA híbrida en OtraEmpresa - Sevilla",
                link="https://www.tecnoempleo.com/ofertas/3",
            ),
        ]
        with patch.object(main.feedparser, "parse", return_value=FakeFeed(entries)):
            offers = main.search_tecnoempleo_rss("QA", "España")

        assert len(offers) == 1
        assert offers[0].mode == "Híbrida"

    def test_no_city_match_falls_back_to_location_argument(self):
        entries = [
            FakeEntry(
                title="Becario/a Soporte TI en EmpresaX",
                link="https://www.tecnoempleo.com/ofertas/4",
            ),
        ]
        with patch.object(main.feedparser, "parse", return_value=FakeFeed(entries)):
            offers = main.search_tecnoempleo_rss("TI", "Alicante")

        assert len(offers) == 1
        # ninguna de las ciudades de la lista fija aparece en el título ->
        # se usa la location pasada como argumento
        assert offers[0].location == "Alicante"
        assert offers[0].company == "EmpresaX"

    def test_title_without_en_keeps_default_company_name(self):
        entries = [
            FakeEntry(title="Becario/a Soporte TI", link="https://www.tecnoempleo.com/ofertas/5")
        ]
        with patch.object(main.feedparser, "parse", return_value=FakeFeed(entries)):
            offers = main.search_tecnoempleo_rss("TI", "Alicante")

        assert offers[0].company == "Tecnoempleo"

    def test_empty_feed_returns_empty_list(self):
        with patch.object(main.feedparser, "parse", return_value=FakeFeed([])):
            offers = main.search_tecnoempleo_rss("DAW", "Granada")

        assert offers == []

    def test_feedparser_exception_returns_empty_list(self):
        with patch.object(main.feedparser, "parse", side_effect=Exception("parse error")):
            offers = main.search_tecnoempleo_rss("DAW", "Granada")

        assert offers == []


# ============================================
# Teamtailor
# ============================================

TEAMTAILOR_RESPONSE_OK = {
    "version": "https://jsonfeed.org/version/1",
    "title": "Freepik/Magnific Jobs",
    "items": [
        {
            "id": "1",
            "url": "https://jobs.magnific.com/jobs/111-backend-engineer-intern",
            "title": "Backend Engineer Intern",
            "content_html": "<p>Buscamos <b>becario/a</b> de backend.</p>",
            "_jobposting": {
                "@type": "JobPosting",
                "title": "Backend Engineer Intern",
                "employmentType": "INTERN",
                "jobLocationType": "TELECOMMUTE",
                "jobLocation": [
                    {
                        "@type": "Place",
                        "address": {
                            "@type": "PostalAddress",
                            "addressLocality": "Madrid",
                            "addressCountry": "ES",
                        }
                    }
                ],
            },
        },
        {
            "id": "2",
            "url": "https://jobs.magnific.com/jobs/222-frontend-intern",
            "title": "Frontend Intern",
            "content_html": "<p>Prácticas de frontend en oficina.</p>",
            "_jobposting": {
                "@type": "JobPosting",
                "title": "Frontend Intern",
                "employmentType": "INTERN",
                "jobLocation": [
                    {
                        "@type": "Place",
                        "address": {
                            "@type": "PostalAddress",
                            "addressLocality": "Barcelona",
                            "addressCountry": "ES",
                        }
                    }
                ],
            },
        },
        {
            "id": "3",
            "url": "https://jobs.magnific.com/jobs/333-sin-localizacion",
            "title": "Data Intern",
            "content_html": "Sin etiquetas HTML aqui.",
            "_jobposting": {
                "@type": "JobPosting",
                "title": "Data Intern",
                "employmentType": "INTERN",
                "jobLocation": [],
            },
        },
    ],
}


class TestSearchTeamtailor:
    def test_parses_offers_from_response(self):
        with patch.object(main.requests, "get", return_value=make_response(200, TEAMTAILOR_RESPONSE_OK)) as get_mock:
            offers = main.search_teamtailor("jobs.magnific.com", "Freepik/Magnific")

        get_mock.assert_called_once()
        args, kwargs = get_mock.call_args
        assert args[0] == "https://jobs.magnific.com/jobs.json"
        assert kwargs["timeout"] == 10

        assert len(offers) == 3
        first = offers[0]
        assert isinstance(first, main.JobOffer)
        assert first.title == "Backend Engineer Intern"
        assert first.company == "Freepik/Magnific"
        assert first.url == "https://jobs.magnific.com/jobs/111-backend-engineer-intern"
        assert first.source == "Teamtailor (Freepik/Magnific)"
        assert first.location == "Madrid"
        # mode "Remoto" solo cuando jobLocationType == TELECOMMUTE
        assert first.mode == "Remoto"
        # las etiquetas HTML de content_html se eliminan
        assert "<p>" not in first.description
        assert "<b>" not in first.description
        assert "becario/a" in first.description

        second = offers[1]
        # jobLocationType ausente -> "No especificado" (nunca "Presencial")
        assert second.mode == "No especificado"
        assert second.location == "Barcelona"

        third = offers[2]
        # jobLocation vacío -> fallback "No especificada"
        assert third.location == "No especificada"
        assert third.mode == "No especificado"
        # content_html sin tags no se ve afectado por el regex de limpieza
        assert third.description == "Sin etiquetas HTML aqui."

    def test_http_error_status_returns_empty_list(self):
        with patch.object(main.requests, "get", return_value=make_response(404, {})):
            offers = main.search_teamtailor("jobs.magnific.com", "Freepik/Magnific")

        assert offers == []

    def test_timeout_returns_empty_list(self):
        with patch.object(main.requests, "get", side_effect=main.requests.exceptions.Timeout("timeout")):
            offers = main.search_teamtailor("jobs.magnific.com", "Freepik/Magnific")

        assert offers == []

    def test_generic_exception_returns_empty_list(self):
        with patch.object(main.requests, "get", side_effect=ValueError("json corrupto")):
            offers = main.search_teamtailor("jobs.magnific.com", "Freepik/Magnific")

        assert offers == []

    def test_empty_items_returns_empty_list(self):
        with patch.object(main.requests, "get", return_value=make_response(200, {"items": []})):
            offers = main.search_teamtailor("jobs.magnific.com", "Freepik/Magnific")

        assert offers == []
