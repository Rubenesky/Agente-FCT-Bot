"""
AGENTE DE BÚSQUEDA DE PRÁCTICAS FCT
=====================================================
Características:
- Búsqueda en fuentes activas: Adzuna, Tecnoempleo, Jooble (requiere
  JOOBLE_API_KEY, opcional), EURES (API pública de la UE, republica los
  datos del SEPE/Empléate), webs de empresas Tier A vía
  Teamtailor (Fase 6: Freepik/Magnific, idealista/AvaiBook, Cívica; el resto de
  empresas Tier A no tienen ATS con feed público conocido, ver plan). Indeed,
  InfoJobs y LinkedIn descartadas por bloqueo anti-bot serio (RSS muertos o
  CAPTCHA), ver notas junto a sus funciones de búsqueda; el SEPE directo
  (RSS por categoría) también descartado - su servidor no responde desde
  IPs de datacenter como las de GitHub Actions (confirmado en vivo
  01/11/2026), de ahí el rodeo vía EURES
- Clasificación Tier A / Tier B / descarte vía API de Gemini (classifier.py)
- Memoria persistente (no repite ofertas vistas)
- Sin aprobación automática: todo lo no descartado se notifica por Telegram
  para revisión humana
- Notificaciones por Telegram
- Logging completo
- Manejo de errores robusto
- MUESTRA LAS OFERTAS EN LA SALIDA ESTÁNDAR
"""

import sys
import io
import logging
import re
import time
import html
from urllib.parse import quote
from datetime import datetime
from typing import TypedDict
from pydantic import BaseModel
from langgraph.graph import StateGraph, START
import requests
import feedparser

from config import (
    ADZUNA_APP_ID, ADZUNA_API_KEY,
    TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
    JOOBLE_API_KEY,
    LOG_FILE, DB_FILE,
    CITIES, KEYWORDS, MAX_CLASSIFICATIONS_PER_RUN
)
from classifier import classify_offer
import db

# ============================================
# SOLUCIÓN PARA ERRORES DE ENCODING EN WINDOWS
# ============================================
if sys.platform == 'win32' and (sys.stdout.encoding or '').lower() != 'utf-8':
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')
    except AttributeError:
        pass

# ============================================
# LOGGING CON ENCODING UTF-8
# ============================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ============================================
# MODELOS Y ESTADO
# ============================================
class JobOffer(BaseModel):
    title: str
    location: str
    mode: str
    company: str
    url: str
    source: str = "Desconocido"
    description: str = ""
    found_date: str = ""
    tier: str = ""
    justification: str = ""
    db_id: int = 0

class State(TypedDict):
    offers: list[JobOffer]
    seen_companies: set[str]
    finished: bool

# ============================================
# NOTIFICACIONES TELEGRAM
# ============================================
def _telegram_post(payload: dict, error_context: str = "Telegram") -> bool:
    """POST genérico a la API de Telegram (sendMessage): construye la URL a
    partir de TELEGRAM_BOT_TOKEN, hace la petición con el payload ya armado
    por la función llamante y centraliza el try/except de logging. Devuelve
    True solo si Telegram respondió 200; cualquier excepción (red, timeout,
    etc.) se loguea y se traduce también en False, nunca se propaga.
    `error_context` conserva el texto exacto que cada función ya logueaba
    (p.ej. "oferta a Telegram") para no perder detalle en los logs."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        response = requests.post(url, json=payload, timeout=10)
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando {error_context}: {e}")
        return False

def send_telegram(message):
    """Envía un mensaje por Telegram."""
    return _telegram_post({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    })

TIER_EMOJI = {"A": "🅰️", "B": "🅱️", "error": "⚠️"}

def format_offer_message(offer) -> str:
    """Construye el texto HTML del mensaje. Todos los campos de la oferta
    vienen de fuentes externas (APIs/RSS de terceros, ofertas publicadas por
    cualquier empresa) y nunca se tratan como confiables: se escapan con
    html.escape antes de interpolarlos, para que un título/descripción con
    '<', '>', '&' o comillas no rompa el parseo HTML de Telegram (que
    devolvería 400 y la oferta se perdería en silencio, igual que pasó con
    el token caído) ni se salga del atributo href='...'."""
    def esc(value) -> str:
        return html.escape(str(value), quote=True)

    emoji = TIER_EMOJI.get(offer.tier, "❔")
    message = f"{emoji} <b>Tier {esc(offer.tier)}</b> — <b>{esc(offer.title[:80])}</b>\n"
    message += f"🏢 {esc(offer.company)}\n"
    message += f"📍 {esc(offer.location)} ({esc(offer.mode)})\n"
    message += f"📌 {esc(offer.source)}\n"
    message += f"💬 {esc(offer.justification)}\n"
    message += f"🔗 <a href='{esc(offer.url)}'>Ver oferta</a>"
    return message

def send_offer_for_review(offer):
    """Envía una oferta individual por Telegram con botones inline
    Aprobar/Descartar. callback_data usa offer.db_id (rowid en SQLite) en vez
    de la URL completa, porque callback_data tiene un límite de 64 bytes."""
    return _telegram_post({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": format_offer_message(offer),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "✅ Aprobar", "callback_data": f"approve:{offer.db_id}"},
                {"text": "❌ Descartar", "callback_data": f"reject:{offer.db_id}"}
            ]]
        }
    }, error_context="oferta a Telegram")

def send_offers_for_review(offers):
    """Envía un aviso con el total, un mensaje individual con botones por
    cada oferta clasificada (Tier A/B), y un único resumen agrupado para las
    que fallaron al clasificar (tier='error') - para no inundar Telegram con
    decenas de mensajes casi idénticos si la API de clasificación falla en
    muchas ofertas seguidas. Las de error siguen quedando 'pending' en la
    base de datos, solo cambia cómo se notifican."""
    if not offers:
        return

    classified = [o for o in offers if o.tier != "error"]
    errored = [o for o in offers if o.tier == "error"]

    send_telegram(
        f"🔍 <b>{len(offers)} nuevas ofertas para revisar</b>\n"
        f"📅 {datetime.now().strftime('%d/%m/%Y %H:%M')}"
    )
    for offer in classified:
        send_offer_for_review(offer)

    if errored:
        preview = "\n".join(f"• {o.title[:60]}" for o in errored[:15])
        extra = f"\n… y {len(errored) - 15} más" if len(errored) > 15 else ""
        send_telegram(
            f"⚠️ <b>{len(errored)} ofertas no se pudieron clasificar</b> "
            f"(error de la API de Gemini).\n"
            f"Quedan guardadas como pendientes en la base de datos.\n\n{preview}{extra}"
        )

def format_followup_message(contact: dict) -> str:
    """Construye el texto del aviso de seguimiento pendiente para un
    contacto (fila de la tabla `contacts`, incluye 'rowid', 'company' y
    'attempt_count'). A partir del 3er intento añade una advertencia para que
    el estudiante se plantee si merece la pena seguir insistiendo."""
    message = (
        f"🔔 Toca hacer seguimiento a <b>{contact['company']}</b> "
        f"(intento nº {contact['attempt_count']})"
    )
    if contact['attempt_count'] >= 3:
        message += (
            "\n⚠️ Este es tu recordatorio del intento nº 3 o posterior — "
            "plantéate si merece la pena seguir insistiendo."
        )
    return message

def send_followup_reminder(contact: dict) -> bool:
    """Envía un aviso de seguimiento pendiente por Telegram con un botón
    inline "❌ Cerrar" (callback_data=close_contact:<rowid>). Reutiliza el
    mismo estilo de request HTTP directo a la API de Telegram que ya usa
    send_offer_for_review, en vez de una librería nueva."""
    return _telegram_post({
        "chat_id": TELEGRAM_CHAT_ID,
        "text": format_followup_message(contact),
        "parse_mode": "HTML",
        "reply_markup": {
            "inline_keyboard": [[
                {
                    "text": "❌ Cerrar (respondió / lo dejo)",
                    "callback_data": f"close_contact:{contact['rowid']}"
                }
            ]]
        }
    }, error_context="recordatorio de seguimiento a Telegram")

def notify_due_followups():
    """Avisa por Telegram de los contactos con seguimiento vencido
    (db.get_due_followups) y limpia next_followup_date de cada uno tras
    notificarlo (db.clear_followup_date), para no repetir el mismo aviso en
    cada ejecución del cron hasta que el usuario reprograme una nueva fecha
    con /seguimiento. NO reprograma automáticamente ni genera borradores de
    mensaje - eso queda fuera de alcance de esta funcionalidad."""
    due = db.get_due_followups()
    if not due:
        return

    logger.info(f"{len(due)} contacto(s) con seguimiento pendiente hoy")
    for contact in due:
        # Solo se limpia next_followup_date si el aviso llegó de verdad - si
        # Telegram falla, se deja la fecha como estaba para que el próximo
        # cron lo reintente, en vez de perder el recordatorio en silencio.
        if send_followup_reminder(contact):
            db.clear_followup_date(contact['rowid'])

# ============================================
# BACKUP DE offers.db
# ============================================
def backup_offers_db_to_telegram() -> bool:
    """Envía el fichero offers.db (DB_FILE) como documento adjunto por
    Telegram al chat configurado en TELEGRAM_CHAT_ID: es la única fuente de
    verdad de la app y solo está versionada en git, así que esto da una
    copia fuera de git, accesible desde el móvil, sin necesitar ninguna
    cuenta/credencial nueva (reutiliza TELEGRAM_BOT_TOKEN).

    Usa sendDocument (multipart/form-data) en vez de _telegram_post
    (sendMessage, que envía JSON) porque adjuntar un fichero requiere
    multipart, no JSON. Mismo manejo de errores que el resto del proyecto:
    nunca propaga la excepción, la loguea con logger.error y devuelve False.
    La cadencia (cuándo se llama) la decide run_agent, no esta función."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendDocument"
        caption = f"🗄️ Backup de {DB_FILE} — {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        with open(DB_FILE, "rb") as db_file:
            response = requests.post(
                url,
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": caption},
                files={"document": (DB_FILE, db_file)},
                timeout=30
            )
        return response.status_code == 200
    except Exception as e:
        logger.error(f"Error enviando backup de {DB_FILE} a Telegram: {e}")
        return False

# ============================================
# FUENTES DE DATOS
# ============================================

# 1. Adzuna API
def search_adzuna(keyword: str, location: str) -> list[JobOffer]:
    """Busca ofertas en Adzuna API."""
    url = "https://api.adzuna.com/v1/api/jobs/es/search/1"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_API_KEY,
        "what": keyword,
        "where": location,
        "results_per_page": 10,
        "content-type": "application/json"
    }
    
    try:
        response = requests.get(url, params=params, timeout=15)
        if response.status_code == 200:
            data = response.json()
            offers = []
            for item in data.get('results', []):
                location_data = item.get('location', {})
                city = location_data.get('display_name', location) if isinstance(location_data, dict) else location
                
                description = item.get('description', '')
                mode = "Presencial"
                if "remoto" in description.lower():
                    mode = "Remoto"
                elif "híbrida" in description.lower() or "hibrida" in description.lower():
                    mode = "Híbrida"
                
                company_data = item.get('company', {})
                company = company_data.get('display_name', 'Empresa desconocida') if isinstance(company_data, dict) else 'Empresa desconocida'
                
                offer = JobOffer(
                    title=item.get('title', 'Sin título'),
                    location=city,
                    mode=mode,
                    company=company,
                    url=item.get('redirect_url', '#'),
                    source="Adzuna",
                    description=description[:500],
                    found_date=datetime.now().isoformat()
                )
                offers.append(offer)
            return offers
        else:
            return []
    except Exception as e:
        logger.error(f"Error en Adzuna: {e}")
        return []

# 2. Tecnoempleo RSS
def search_tecnoempleo_rss(keyword: str, location: str) -> list[JobOffer]:
    """Busca ofertas en Tecnoempleo RSS."""
    try:
        clean_keyword = keyword.replace(" ", "+")
        rss_url = f"https://www.tecnoempleo.com/rss/empleo/?q={clean_keyword}"
        feed = feedparser.parse(rss_url)
        offers = []
        for entry in feed.entries[:10]:
            title = entry.title
            link = entry.link
            
            extracted_location = location
            for city in ["Granada", "Málaga", "Sevilla", "Barcelona", "Madrid", "Valencia", "Bilbao"]:
                if city in title:
                    extracted_location = city
                    break
            
            mode = "Presencial"
            if "remoto" in title.lower():
                mode = "Remoto"
            elif "híbrida" in title.lower() or "hibrida" in title.lower():
                mode = "Híbrida"
            
            company = "Tecnoempleo"
            if " en " in title:
                parts = title.split(" en ")
                if len(parts) > 1:
                    company = parts[1].split(" - ")[0].strip()
            
            offer = JobOffer(
                title=title,
                location=extracted_location,
                mode=mode,
                company=company,
                url=link,
                source="Tecnoempleo",
                description=title,
                found_date=datetime.now().isoformat()
            )
            offers.append(offer)
        return offers
    except Exception as e:
        logger.error(f"Error en Tecnoempleo: {e}")
        return []

# 3. Indeed (PENDIENTE - fuera de la lista de fuentes activas)
# rss.indeed.com devuelve 404/403 en todos los dominios probados (.com, es.indeed.com):
# el feed RSS está deprecado. Se deja la función sin usar hasta encontrar una vía
# alternativa (ver plan Fase 1).
def search_indeed_rss(keyword: str, location: str) -> list[JobOffer]:
    """Busca ofertas en Indeed RSS - INACTIVA, ver nota arriba."""
    try:
        clean_keyword = keyword.replace(" ", "+")
        rss_url = f"https://rss.indeed.com/rss?q={clean_keyword}&l={location}"
        feed = feedparser.parse(rss_url)
        offers = []
        for entry in feed.entries[:5]:
            offer = JobOffer(
                title=entry.title,
                location=location,
                mode="Presencial",
                company="Indeed",
                url=entry.link,
                source="Indeed",
                description=entry.title,
                found_date=datetime.now().isoformat()
            )
            offers.append(offer)
        return offers
    except Exception as e:
        logger.error(f"Error en Indeed: {e}")
        return []

# 4. InfoJobs (PENDIENTE - fuera de la lista de fuentes activas)
# infojobs.net bloquea peticiones simples con 405 en todo el dominio (protección
# anti-bot). Existe API oficial de partners en developer.infojobs.net que requiere
# registro. Se deja la función sin usar hasta integrar esa API (ver plan Fase 1/6).
def search_infojobs_rss(keyword: str, location: str) -> list[JobOffer]:
    """Busca ofertas en InfoJobs (RSS) - INACTIVA, ver nota arriba."""
    try:
        clean_keyword = keyword.replace(" ", "+")
        rss_url = f"https://www.infojobs.net/rss/offers?q={clean_keyword}&l={location}"
        feed = feedparser.parse(rss_url)
        offers = []
        for entry in feed.entries[:5]:
            offer = JobOffer(
                title=entry.title,
                location=location,
                mode="Presencial",
                company="InfoJobs",
                url=entry.link,
                source="InfoJobs",
                description=entry.title,
                found_date=datetime.now().isoformat()
            )
            offers.append(offer)
        return offers
    except Exception as e:
        logger.error(f"Error en InfoJobs: {e}")
        return []

# 5. Webs de empresas Tier A (Fase 6 - piloto)
# Empresas que usan Teamtailor como ATS exponen un feed JSON público en
# https://<subdominio>/jobs.json (formato JSON Feed + extensión _jobposting
# con schema.org JobPosting, incluyendo jobLocation estructurado). No hace
# falta autenticación ni scraping de HTML. Devuelve TODAS las ofertas
# abiertas de la empresa (no admite filtrar por keyword), así que se llama
# una sola vez por ejecución, no dentro del bucle de ciudades/keywords.
TIER_A_TEAMTAILOR_SOURCES = [
    ("jobs.magnific.com", "Freepik/Magnific"),
    ("idealista.teamtailor.com", "idealista/AvaiBook"),
    ("empleo.civica-soft.com", "Cívica"),
]

def search_teamtailor(subdomain: str, company_label: str) -> list[JobOffer]:
    """Busca todas las ofertas abiertas de una empresa Tier A que usa
    Teamtailor, vía su feed JSON público."""
    try:
        url = f"https://{subdomain}/jobs.json"
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return []
        data = response.json()
        offers = []
        for item in data.get('items', []):
            posting = item.get('_jobposting', {})
            job_locations = posting.get('jobLocation', [])
            if job_locations:
                address = job_locations[0].get('address', {})
                location = address.get('addressLocality') or 'No especificada'
            else:
                location = 'No especificada'

            mode = "Remoto" if posting.get('jobLocationType') == 'TELECOMMUTE' else "No especificado"

            description = re.sub('<[^<]+?>', ' ', item.get('content_html', ''))
            offer = JobOffer(
                title=item.get('title', 'Sin título'),
                location=location,
                mode=mode,
                company=company_label,
                url=item.get('url', '#'),
                source=f"Teamtailor ({company_label})",
                description=description[:1500],
                found_date=datetime.now().isoformat()
            )
            offers.append(offer)
        return offers
    except Exception as e:
        logger.error(f"Error en Teamtailor ({company_label}): {e}")
        return []

# 6. Jooble (agregador con cobertura España, API con key - opcional)
# Requiere JOOBLE_API_KEY (config.py, opcional): se consigue registrándose
# en es.jooble.org/api/about (registro self-service, la key llega por
# email). Mientras el usuario no la configure, esta función no hace ninguna
# llamada HTTP: devuelve [] de inmediato. El aviso de "fuente desactivada"
# se loguea una sola vez por ejecución desde search_node (no aquí), para no
# repetirlo en cada combinación de ciudad/keyword del bucle.
#
# Esquema de petición/respuesta confirmado en la documentación oficial
# (help.jooble.org/en/support/solutions/articles/60001448238-rest-api-documentation):
# POST https://es.jooble.org/api/{key} con body {"keywords", "location"};
# respuesta {"totalCount": int, "jobs": [{"id", "title", "location",
# "snippet", "salary", "source", "type", "link", "company", "updated"}]}.
# Sí filtra por ubicación española, así que se llama dentro del mismo bucle
# CITIES x KEYWORDS que Adzuna/Tecnoempleo.
def search_jooble(keyword: str, location: str) -> list[JobOffer]:
    """Busca ofertas en la API de Jooble (dominio es.jooble.org, cobertura
    España). Sin JOOBLE_API_KEY configurada, devuelve [] sin hacer ninguna
    petición HTTP."""
    if not JOOBLE_API_KEY:
        return []
    try:
        url = f"https://es.jooble.org/api/{JOOBLE_API_KEY}"
        response = requests.post(url, json={
            "keywords": keyword,
            "location": location
        }, timeout=15)
        if response.status_code != 200:
            return []
        data = response.json()
        offers = []
        for item in data.get('jobs', []):
            snippet = item.get('snippet', '')
            mode = "Presencial"
            if "remoto" in snippet.lower():
                mode = "Remoto"
            elif "híbrida" in snippet.lower() or "hibrida" in snippet.lower():
                mode = "Híbrida"

            offer = JobOffer(
                title=item.get('title', 'Sin título'),
                location=item.get('location', location),
                mode=mode,
                company=item.get('company', 'Empresa desconocida'),
                url=item.get('link', '#'),
                source="Jooble",
                description=snippet[:500],
                found_date=datetime.now().isoformat()
            )
            offers.append(offer)
        return offers
    except Exception as e:
        logger.error(f"Error en Jooble: {e}")
        return []

# 7. EURES (portal de empleo de la Unión Europea, API pública sin key)
# Endpoint: POST https://europa.eu/eures/api/jv-searchengine/public/jv-search/search.
# Documentación no oficial (reverse-engineered, github.com/rorar/EURES-API-Documentation)
# - no hay garantía de estabilidad por parte de la Comisión Europea, pero el
# endpoint en sí es de europa.eu, no de un tercero. Confirmado en vivo
# (01/11/2026) que cada oferta trae "source": "empleate": EURES republica
# los mismos datos del SEPE/Empléate, con la ventaja de que su servidor sí
# responde desde IPs de datacenter (el SEPE directo no, ver nota en
# search_node). El nombre de la empresa no viene en el listado (igual que
# en el SEPE directo, redactado por privacidad en buena parte de las
# ofertas) - se deja un literal fijo en vez de un dato inventado.
#
# El filtro locationCodes solo funciona de forma fiable a nivel NUTS2
# (comunidad autónoma, ej. "es61" = Andalucía); probado en vivo que
# filtrar directamente por NUTS3 (ej. "ES614" = Granada) devuelve siempre
# 0 resultados pese a que esos mismos códigos sí aparecen en el campo
# locationMap de la respuesta. Por eso se pide toda Andalucía y se filtra
# a mano por EURES_NUTS3_BY_CITY, quedándose solo con Granada/Málaga.
#
# La URL de detalle (https://europa.eu/eures/portal/jv-se/jv-details/{id})
# se confirmó contra un resultado real indexado públicamente - es una SPA,
# pero carga el contenido igual en un navegador normal.
EURES_REGION_CODE = "es61"  # Andalucía (NUTS2)
EURES_NUTS3_BY_CITY = {"Granada": "ES614", "Málaga": "ES617"}  # NUTS3

def search_eures(keyword: str) -> list[JobOffer]:
    """Busca ofertas en la API pública de EURES, filtrando por Andalucía y
    quedándose solo con las de Granada/Málaga (ver nota arriba)."""
    try:
        url = "https://europa.eu/eures/api/jv-searchengine/public/jv-search/search"
        payload = {
            "resultsPerPage": 10,
            "page": 1,
            "sortSearch": "BEST_MATCH",
            "keywords": [{"keyword": keyword, "specificSearchCode": "TITLE"}],
            "publicationPeriod": "LAST_MONTH",
            "occupationUris": [],
            "skillUris": [],
            "requiredExperienceCodes": [],
            "positionScheduleCodes": [],
            "sectorCodes": [],
            "educationAndQualificationLevelCodes": [],
            "positionOfferingCodes": [],
            "locationCodes": [EURES_REGION_CODE],
            "euresFlagCodes": [],
            "otherBenefitsCodes": [],
            "requiredLanguages": [],
            "minNumberPost": None,
            "sessionId": f"agente-fct-{keyword}",
            "requestLanguage": "es",
        }
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            return []
        data = response.json()
        offers = []
        for jv in data.get('jvs', []):
            nuts3_codes = jv.get('locationMap', {}).get('ES', [])
            city = next(
                (c for c, code in EURES_NUTS3_BY_CITY.items() if code in nuts3_codes),
                None
            )
            if city is None:
                continue

            # El id lo genera la propia API de EURES, pero al venir de una
            # documentación no oficial (reverse-engineered) se trata como
            # dato externo no confiable: se descarta la oferta si falta, y
            # se url-encodea (quote, safe='') antes de meterlo en la URL
            # para que no pueda romper la ruta si algún día trae caracteres
            # inesperados.
            jv_id = str(jv.get('id') or '')
            if not jv_id:
                continue

            description = jv.get('description') or ''
            mode = "Presencial"
            if "teletrabajo" in description.lower() or "remoto" in description.lower():
                mode = "Remoto"
            elif "híbrid" in description.lower() or "hibrid" in description.lower():
                mode = "Híbrida"

            offer = JobOffer(
                title=jv.get('title', 'Sin título'),
                location=city,
                mode=mode,
                company="Empresa no especificada",
                url=f"https://europa.eu/eures/portal/jv-se/jv-details/{quote(jv_id, safe='')}?lang=es",
                source="EURES",
                description=description[:500],
                found_date=datetime.now().isoformat()
            )
            offers.append(offer)
        return offers
    except Exception as e:
        logger.error(f"Error en EURES: {e}")
        return []

# ============================================
# NODOS LANGGRAPH
# ============================================
def search_node(state: State) -> State:
    """Nodo de búsqueda principal con las fuentes activas."""
    all_found = []

    # Aviso de una sola vez por ejecución si Jooble está desactivada por
    # falta de API key - fuera del bucle de ciudades/keywords para no
    # repetirlo en cada combinación (sería spam en los logs).
    if not JOOBLE_API_KEY:
        logger.warning(
            "Fuente Jooble desactivada: falta JOOBLE_API_KEY (ver .env.example "
            "para instrucciones de cómo conseguirla)."
        )

    for city in CITIES:
        logger.info(f"📌 Buscando en {city}...")

        # 3. Jooble - UNA sola llamada por ciudad (no por cada keyword),
        # combinando los términos más relevantes en una sola query (probado
        # en vivo: Jooble trata varias palabras como búsqueda ampliada, no
        # exige coincidencia exacta). La cuenta de Jooble del usuario tiene
        # un límite de 500 solicitudes cuyo periodo (¿diario? ¿total?) no
        # se aclaraba en el aviso de alta - con 28 llamadas/ejecución
        # (2 ciudades x 14 keywords) se agotaría en pocos días si fuera un
        # límite total. A 1 por ciudad (2/ejecución, 4/día) el margen de
        # seguridad es mucho mayor bajo cualquier interpretación del límite.
        # Sin JOOBLE_API_KEY, search_jooble devuelve [] sin hacer ninguna
        # petición HTTP - ver aviso ya logueado arriba.
        jooble_query = "FCT practicas DAW desarrollo programacion"
        jooble_offers = search_jooble(jooble_query, city)
        if jooble_offers:
            logger.info(f"    + Jooble: {len(jooble_offers)} ofertas")
            all_found.extend(jooble_offers)

        for keyword in KEYWORDS:
            logger.info(f"  - {keyword}")

            # 1. Adzuna
            adzuna_offers = search_adzuna(keyword, city)
            if adzuna_offers:
                logger.info(f"    + Adzuna: {len(adzuna_offers)} ofertas")
                all_found.extend(adzuna_offers)

            # 2. Tecnoempleo
            tecno_offers = search_tecnoempleo_rss(keyword, city)
            if tecno_offers:
                logger.info(f"    + Tecnoempleo: {len(tecno_offers)} ofertas")
                all_found.extend(tecno_offers)

            # Indeed e InfoJobs: descartadas por bloqueo anti-bot serio (RSS
            # muertos o CAPTCHA), ver notas junto a search_indeed_rss /
            # search_infojobs_rss más arriba. LinkedIn se descartó por el
            # mismo motivo. Jooble se añadió como reemplazo de cobertura
            # (13/09/2026); se investigará SEPE/otras fuentes más adelante.

            time.sleep(0.5)

    # 4. Webs de empresas Tier A (Teamtailor) - fuera del bucle de ciudades/
    # keywords: el feed devuelve todas las ofertas abiertas de golpe, no
    # admite búsqueda por keyword.
    for subdomain, company_label in TIER_A_TEAMTAILOR_SOURCES:
        tt_offers = search_teamtailor(subdomain, company_label)
        if tt_offers:
            logger.info(f"    + Teamtailor ({company_label}): {len(tt_offers)} ofertas")
            all_found.extend(tt_offers)

    # 5. EURES - también fuera del bucle de ciudades: filtra por toda
    # Andalucía (ver nota junto a search_eures sobre por qué no se puede
    # filtrar directamente por Granada/Málaga) y la propia función ya
    # descarta client-side lo que no sea de esas dos ciudades.
    for keyword in KEYWORDS:
        eures_offers = search_eures(keyword)
        if eures_offers:
            logger.info(f"    + EURES ({keyword}): {len(eures_offers)} ofertas")
            all_found.extend(eures_offers)
        time.sleep(0.5)

    logger.info(f"Resumen: {len(all_found)} ofertas totales encontradas")

    # Filtrar ofertas ya vistas (persistidas en SQLite - db.save_offer las
    # marca como vistas en classify_node, una vez clasificadas)
    new_offers = [o for o in all_found if not db.is_seen(o.url)]

    # Eliminar duplicados por URL
    seen_urls = set()
    unique_offers = []
    for offer in new_offers:
        if offer.url not in seen_urls:
            seen_urls.add(offer.url)
            unique_offers.append(offer)

    if not unique_offers:
        logger.info("No se encontraron ofertas nuevas.")
    else:
        logger.info(f"Total ofertas nuevas encontradas: {len(unique_offers)}")
        
        # ============================================
        # NUEVO: MOSTRAR OFERTAS EN LA SALIDA ESTÁNDAR
        # ============================================
        print("\n--- OFERTAS NUEVAS ENCONTRADAS ---")
        for i, offer in enumerate(unique_offers[:10], 1):
            print(f"{i}. {offer.title[:80]}")
            print(f"   Empresa: {offer.company}")
            print(f"   Ubicación: {offer.location}")
            print(f"   Modalidad: {offer.mode}")
            print(f"   Fuente: {offer.source}")
            print(f"   🔗 {offer.url}")
            print("")
    
    state['offers'].extend(unique_offers)
    return state

def filter_node(state: State) -> State:
    """Filtra ofertas duplicadas por empresa."""
    filtered_offers = []
    for offer in state['offers']:
        if offer.company not in state['seen_companies']:
            filtered_offers.append(offer)
            state['seen_companies'].add(offer.company)
    state['offers'] = filtered_offers
    return state

def reflect_node(state: State) -> State:
    """Nodo de reflexión."""
    return state

def classify_node(state: State) -> State:
    """
    Clasifica cada oferta con el clasificador Tier A/B/descarte (API de Gemini)
    y la guarda en SQLite (db.save_offer) - esto es lo que la marca como "vista"
    para futuras ejecuciones. No hay aprobación automática: todo lo que no sea
    "descarte" según los criterios definidos se notifica por Telegram y queda
    con status 'pending' hasta que el usuario decida (Fase 4). Un fallo del
    clasificador nunca se trata como descarte (ver classify_offer).

    Cupo por ejecución (MAX_CLASSIFICATIONS_PER_RUN): además de las ofertas
    nuevas de esta corrida, se reintentan ofertas que quedaron en tier='error'
    en ejecuciones anteriores (antes esto era el script manual
    reclassify_errors.py, ahora ya no hace falta ejecutarlo a mano). Ambos
    grupos comparten el mismo cupo total para no multiplicar las llamadas a
    Gemini por ejecución. Se reserva hasta un tercio del cupo para reintentos
    de error - así un backlog grande de errores no se come todo el cupo de
    ofertas nuevas, pero tampoco se queda indefinidamente sin avanzar; si hay
    menos errores pendientes que ese tercio, el resto se lo quedan las
    ofertas nuevas. Las ofertas nuevas que no entran en el cupo de esta
    ejecución NO se guardan en la base de datos, así que search_node las
    volverá a encontrar (no están marcadas como "vistas") y se clasificarán
    en la siguiente ejecución del cron.
    """
    error_quota = max(1, MAX_CLASSIFICATIONS_PER_RUN // 3) if MAX_CLASSIFICATIONS_PER_RUN > 0 else 0
    error_rows = db.get_error_offers(limit=error_quota)
    new_quota = max(MAX_CLASSIFICATIONS_PER_RUN - len(error_rows), 0)

    new_offers = state['offers'][:new_quota]
    deferred = len(state['offers']) - len(new_offers)
    if deferred > 0:
        logger.info(
            f"{deferred} ofertas nuevas superan el cupo de {MAX_CLASSIFICATIONS_PER_RUN} "
            f"por ejecución, quedan para la siguiente corrida del cron."
        )

    error_offers = [
        JobOffer(
            title=row['title'], location=row['location'], mode=row['mode'],
            company=row['company'], url=row['url'], source=row['source'],
            description=row['description'] or '', found_date=row['found_date'],
            db_id=row['rowid'],
        )
        for row in error_rows
    ]

    to_classify = new_offers + error_offers

    if not to_classify:
        logger.info("No hay ofertas nuevas ni errores pendientes que reintentar.")
        state['offers'] = []
        state['finished'] = True
        return state

    logger.info(
        f"Clasificando {len(to_classify)} ofertas "
        f"({len(new_offers)} nuevas, {len(error_offers)} reintentos de tier='error')..."
    )

    to_review = []
    for i, offer in enumerate(to_classify):
        if i > 0:
            # Ritmo entre llamadas: el nivel gratuito de Gemini admite solo
            # ~10-15 peticiones/min. classify_offer ya reintenta ante 429,
            # pero espaciar las llamadas evita depender solo de eso.
            time.sleep(5)
        tier, justification = classify_offer(offer)
        offer.tier = tier
        offer.justification = justification

        if offer.db_id:
            # Ya existía en la base de datos (reintento de una oferta en
            # 'error'): se actualiza la fila existente en vez de insertar
            # una nueva.
            db.update_offer_classification(offer.db_id, tier, justification)
        else:
            offer.db_id = db.save_offer(offer)

        if tier == "descarte":
            logger.info(f"❌ Descartada: {offer.title[:50]} — {justification}")
        elif tier == "error":
            logger.warning(f"⚠️ Sigue en error: {offer.title[:50]} — {justification}")
        else:
            logger.info(f"✅ Tier {tier}: {offer.title[:50]} — {justification}")
            to_review.append(offer)

    if to_review:
        send_offers_for_review(to_review)
    else:
        logger.info("Ninguna oferta pasó la clasificación")

    state['offers'] = to_review
    state['finished'] = True
    return state

# ============================================
# CONSTRUCCIÓN DEL GRAFO
# ============================================
def create_graph():
    """Crea y devuelve el grafo LangGraph.

    Pipeline lineal: search -> filter -> reflect -> classify, sin bucle de
    reintento. search_node consulta fuentes deterministas (Adzuna,
    Tecnoempleo RSS, Teamtailor): dentro de una misma ejecución, las mismas
    keywords contra las mismas fuentes en el mismo momento siempre devuelven
    lo mismo, y nada marca ofertas como "vistas" hasta classify_node (vía
    db.save_offer). Repetir la búsqueda antes de clasificar no podía
    encontrar nada nuevo, solo repetía ~59 llamadas HTTP idénticas por
    intento extra."""
    graph = StateGraph(State)

    graph.add_node("search_node", search_node)
    graph.add_node("filter_node", filter_node)
    graph.add_node("reflect_node", reflect_node)
    graph.add_node("classify_node", classify_node)

    graph.add_edge(START, "search_node")
    graph.add_edge("search_node", "filter_node")
    graph.add_edge("filter_node", "reflect_node")
    graph.add_edge("reflect_node", "classify_node")

    return graph.compile()

# ============================================
# FUNCIÓN PRINCIPAL
# ============================================
def run_agent():
    """Ejecuta el agente una vez."""
    logger.info("=" * 50)
    logger.info("Iniciando agente de busqueda de practicas FCT")
    logger.info(datetime.now().strftime('%d/%m/%Y %H:%M:%S'))
    logger.info("=" * 50)

    db.init_db()

    app = create_graph()
    
    initial_state: State = {
        "offers": [],
        "seen_companies": set(),
        "finished": False
    }
    
    try:
        result = app.invoke(initial_state)
        logger.info(f"Ejecucion completada: {len(result['offers'])} ofertas notificadas para revisión")

        # Recordatorio de seguimiento: se llama al final del flujo, ya dentro
        # de esta misma ejecución del cron, para que el commit final de
        # offers.db que hace el workflow de GitHub Actions incluya también
        # los cambios de next_followup_date/status de la tabla `contacts`.
        notify_due_followups()

        # Backup de offers.db por Telegram: no en cada ejecución (el cron
        # corre 2 veces al día, sería ruido innecesario enviar el fichero
        # completo cada vez), sino una cadencia semanal de bajo
        # mantenimiento. Se elige domingo (weekday() == 6) por ser, de las
        # opciones igual de simples, el día de menor actividad de ofertas
        # nuevas para revisar. Deliberadamente sin estado adicional (nada de
        # tabla/fichero de "última vez que se hizo backup"): comprobar el
        # día de la semana con datetime.now() basta y no añade complejidad.
        # Nota: como el cron corre 2 veces ese domingo (9:00 y 21:00), el
        # backup se envía 2 veces ese día - aceptable por mantenerlo simple.
        # Un fallo aquí (red, Telegram caído, etc.) se loguea pero nunca
        # debe hacer fallar la ejecución completa: la búsqueda/clasificación
        # ya se hizo bien, el backup es un extra.
        if datetime.now().weekday() == 6:
            try:
                backup_offers_db_to_telegram()
            except Exception as e:
                logger.error(f"Error inesperado en el backup semanal de offers.db: {e}")

        return result
    except Exception as e:
        logger.error(f"Error en la ejecucion: {e}")
        send_telegram(f"❌ Error en el agente: {e}")
        return None

# ============================================
# EJECUCIÓN PRINCIPAL
# ============================================
# El scheduler vive en GitHub Actions (.github/workflows/search.yml), no aquí.
# main.py siempre se ejecuta una sola vez y termina.
if __name__ == "__main__":
    run_agent()