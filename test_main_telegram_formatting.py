"""test_main_telegram_formatting.py - Pruebas de format_offer_message
(main.py).

Todos los campos de una oferta vienen de fuentes externas (APIs/RSS de
terceros, ofertas publicadas por cualquier empresa), así que nunca se tratan
como confiables al construir el HTML del mensaje de Telegram: un título,
empresa o justificación con '<', '>', '&' o comillas sin escapar rompería el
parseo HTML de Telegram (sendMessage devolvería 400 y la oferta se perdería
en silencio) o se saldría del atributo href='...'. Estas pruebas confirman
que format_offer_message escapa esos campos con html.escape antes de
interpolarlos.
"""
import main


def make_offer(**overrides):
    defaults = dict(
        title="Backend Developer",
        location="Granada",
        mode="Presencial",
        company="Empresa Ejemplo",
        url="https://example.com/oferta/1",
        source="Adzuna",
        description="",
        found_date="2026-11-01T00:00:00",
        tier="B",
        justification="Encaja con el perfil DAW.",
    )
    defaults.update(overrides)
    return main.JobOffer(**defaults)


class TestFormatOfferMessageEscaping:
    def test_escapes_html_special_chars_in_title_and_company(self):
        offer = make_offer(
            title="Dev <script>alert(1)</script> & Co",
            company="R&D <Innovación>",
        )
        message = main.format_offer_message(offer)

        assert "<script>" not in message
        assert "&amp;" in message
        assert "&lt;script&gt;" in message
        assert "R&amp;D &lt;Innovación&gt;" in message

    def test_escapes_single_quote_in_url_so_it_cannot_break_href_attribute(self):
        offer = make_offer(url="https://example.com/oferta?x=1'onmouseover='alert(1)")
        message = main.format_offer_message(offer)

        # La comilla simple cruda rompería el atributo href='...'; escapada
        # (&#x27;) el valor del atributo sigue siendo uno solo.
        assert "href='https://example.com/oferta?x=1'onmouseover=" not in message
        assert "&#x27;" in message

    def test_escapes_justification_and_location_fields(self):
        offer = make_offer(
            justification="Cumple el <requisito> de \"ubicación\" & perfil",
            location="Granada & Málaga",
        )
        message = main.format_offer_message(offer)

        assert "<requisito>" not in message
        assert "&lt;requisito&gt;" in message
        assert "Granada &amp; Málaga" in message

    def test_plain_offer_still_renders_readable_message(self):
        offer = make_offer()
        message = main.format_offer_message(offer)

        assert "Backend Developer" in message
        assert "Empresa Ejemplo" in message
        assert "<a href='https://example.com/oferta/1'>Ver oferta</a>" in message


class TestSearchEuresSkipsOffersWithoutId:
    def test_offer_missing_id_is_skipped(self):
        from unittest.mock import MagicMock, patch

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "jvs": [
                {
                    "id": "",
                    "title": "Sin ID",
                    "description": "",
                    "locationMap": {"ES": ["ES614"]},
                },
                {
                    "id": "VALID123",
                    "title": "Con ID",
                    "description": "",
                    "locationMap": {"ES": ["ES614"]},
                },
            ]
        }
        with patch.object(main.requests, "post", return_value=response):
            offers = main.search_eures("desarrollador")

        assert len(offers) == 1
        assert offers[0].title == "Con ID"

    def test_id_with_unsafe_chars_is_url_encoded(self):
        from unittest.mock import MagicMock, patch

        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "jvs": [
                {
                    "id": "a/b?c=1&d",
                    "title": "Oferta con id raro",
                    "description": "",
                    "locationMap": {"ES": ["ES617"]},
                }
            ]
        }
        with patch.object(main.requests, "post", return_value=response):
            offers = main.search_eures("desarrollador")

        assert len(offers) == 1
        assert "/" not in offers[0].url.split("jv-details/")[1].split("?lang=es")[0]
        assert offers[0].url.startswith("https://europa.eu/eures/portal/jv-se/jv-details/a%2Fb%3Fc%3D1%26d")
