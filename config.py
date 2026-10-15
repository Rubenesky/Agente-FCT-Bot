# config.py
# ============================================
# CONFIGURACIÓN DEL AGENTE
# ============================================
import os
from dotenv import load_dotenv

load_dotenv()

ADZUNA_APP_ID = os.environ["ADZUNA_APP_ID"]
ADZUNA_API_KEY = os.environ["ADZUNA_API_KEY"]

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

GROQ_API_KEY = os.environ["GROQ_API_KEY"]

# API key de OpenRouter, usada por classifier.py como respaldo si Groq falla
# del todo. Opcional: sigue el mismo patrón que JOOBLE_API_KEY - si no está
# configurada, el respaldo se omite (con un log de aviso) y la clasificación
# depende solo de Groq, sin romper nada.
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# Token de GitHub (Personal Access Token con permiso de escritura sobre el
# repo) usado por controller.py/git_sync.py para comitear y pushear
# offers.db a git tras cada aprobación/descarte desde Telegram, ya que
# Render no tiene disco persistente (ver render.yaml) y el filesystem se
# resetea en cada redeploy. Opcional: si no está configurado, el bot sigue
# funcionando con normalidad, pero las decisiones no se sincronizan con git
# (se pierden en el próximo redeploy). Ver .env.example para instrucciones
# de cómo generarlo.
GIT_PUSH_TOKEN = os.environ.get("GIT_PUSH_TOKEN")

# API key de Jooble (agregador con cobertura España) usada por
# search_jooble en main.py. Opcional: el usuario aún no se ha registrado.
# Se consigue en es.jooble.org/api/about (registro self-service, la key
# llega por email). Sin ella, search_jooble no hace ninguna llamada HTTP y
# devuelve [] (esa fuente queda desactivada hasta configurarla).
JOOBLE_API_KEY = os.environ.get("JOOBLE_API_KEY")

# Archivos
DB_FILE = "offers.db"
LOG_FILE = "agente.log"


# Configuración de búsqueda
CITIES = ["Granada", "Málaga"]
KEYWORDS = [
    "FCT", "prácticas", "prácticas FP", "DAW", "DAM", "ASIR",
    "desarrollo", "programación", "becario", "beca",
    "estudiante", "junior", "trainee", "informática", "sistemas"
]
# Tope de ofertas que se clasifican (llamadas a Groq) en una sola ejecución
# del cron - el resto se deja para la siguiente ejecución en vez de intentar
# clasificarlas todas de golpe (se llegó a ver una tanda de 74 ofertas nuevas
# en una sola corrida, lo que agota la cuota). Configurable por
# variable de entorno para poder ajustarlo sin tocar código.
MAX_CLASSIFICATIONS_PER_RUN = int(os.environ.get("MAX_CLASSIFICATIONS_PER_RUN", "30"))

# Empresas Tier A ya mapeadas (Granada/Málaga)
TIER_A_COMPANIES = [
    "Cívica", "Nazaríes", "NTT Data", "Firmafy", "Atlax 360",
    "AvaiBook", "idealista", "Freepik", "Magnific", "Yerbabuena",
    "Aircury", "5 Digital Street", "Ideable", "MailerLite", "Nexion", "Oftex"
]