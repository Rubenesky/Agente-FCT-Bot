# Agente FCT

Bot autónomo que busca ofertas de prácticas (FCT) para un estudiante de CFGS de
Desarrollo de Aplicaciones Web (DAW) en Granada/Málaga, las clasifica con
Groq (con OpenRouter como respaldo), las manda por Telegram para aprobación
humana, y ayuda a llevar el seguimiento de los contactos con las empresas.

## Qué hace

1. **Busca** ofertas 2 veces al día (cron de GitHub Actions) en Adzuna,
   Tecnoempleo (RSS), Jooble (agregador con cobertura España, requiere
   `JOOBLE_API_KEY` opcional), Himalayas (ofertas remotas, filtro nativo
   Entry-level) y las webs de empleo (Teamtailor) de un puñado de
   empresas Tier A ya mapeadas (Cívica, idealista/AvaiBook, Freepik/Magnific).
2. **Clasifica** cada oferta nueva con Groq (nivel gratuito, API compatible
   con OpenAI; OpenRouter como respaldo si Groq falla) en `Tier A` (empresa ya
   mapeada + ubicación válida + encaja en el perfil DAW), `Tier B` (mismo
   criterio de contenido/ubicación pero empresa nueva) o `descarte`.
3. **Notifica** por Telegram cada oferta Tier A/B con botones
   ✅ Aprobar / ❌ Descartar.
4. **Recuerda** hacer seguimiento a los contactos con los que ya hablaste:
   registras una fecha con `/seguimiento` y el bot te avisa solo cuando toca.

Todo el estado (ofertas vistas, decisiones, contactos) vive en un único
fichero SQLite (`offers.db`) que se versiona en git, para que tanto el cron
(GitHub Actions) como el bot (Render) trabajen sobre el mismo dato sin
necesitar una base de datos gestionada aparte.

## Arquitectura

| Componente | Dónde corre | Qué hace |
|---|---|---|
| `main.py` | GitHub Actions (cron, 2x/día) | Grafo de LangGraph: busca ofertas nuevas → las clasifica con Groq/OpenRouter → notifica por Telegram → avisa de seguimientos vencidos. Al final del workflow se comitea y pushea `offers.db`. |
| `classifier.py` | (llamado desde `main.py`) | Llama a Groq (`llama-3.3-70b-versatile`, nivel gratuito) para clasificar una oferta. Reintenta en 429 usando el header `Retry-After` que devuelve la propia API, con backoff exponencial + jitter como respaldo. Si Groq agota los reintentos o falla con un error no recuperable, recurre una vez a OpenRouter (`google/gemma-4-31b-it:free`) como respaldo, solo si `OPENROUTER_API_KEY` está configurada. |
| `controller.py` | Render (servicio web, siempre encendido) | Bot de Telegram: comandos `/start`, `/status`, `/seguimiento`, y los botones Aprobar/Descartar/Cerrar. |
| `db.py` | Ambos | Acceso a `offers.db` (SQLite): tabla `offers` (ofertas + decisión humana) y tabla `contacts` (seguimiento a empresas). |
| `git_sync.py` | Render (vía `controller.py`) | Como Render no tiene disco persistente, cada decisión tomada desde Telegram se comitea y pushea a git al momento (`git add` + `commit` + `pull --rebase --autostash` + `push`), para que no se pierda en el próximo redeploy. |
| `config.py` | Ambos | Variables de entorno y parámetros de búsqueda/clasificación. |

## Comandos de Telegram

- `/start` — descripción del bot y comandos disponibles.
- `/status` — cuántas ofertas están pendientes de revisión.
- `/seguimiento Empresa X | 17/09/2026 | nota opcional` — registra o
  reprograma un recordatorio de seguimiento para esa empresa (la nota es
  opcional; la fecha debe ir en formato `dd/mm/aaaa`). Cada vez que se llama
  para la misma empresa (sin distinguir mayúsculas/minúsculas) se cuenta como
  un intento más.
- Botones inline:
  - **✅ Aprobar / ❌ Descartar** en cada oferta nueva.
  - **❌ Cerrar (respondió / lo dejo)** en cada aviso de seguimiento.

A partir del 3er intento de seguimiento con la misma empresa, el aviso
incluye una advertencia explícita para plantearte si merece la pena seguir
insistiendo. El bot **no** genera ni envía mensajes de seguimiento por ti —
solo te recuerda que toca escribir tú.

Solo el chat configurado en `TELEGRAM_CHAT_ID` puede usar el bot; cualquier
otro chat_id se ignora (con log de aviso).

## Variables de entorno

Ver `.env.example`. Resumen:

| Variable | Obligatoria | Para qué |
|---|---|---|
| `ADZUNA_APP_ID` / `ADZUNA_API_KEY` | Sí | Búsqueda en la API de Adzuna. |
| `TELEGRAM_BOT_TOKEN` | Sí | Token del bot (BotFather). |
| `TELEGRAM_CHAT_ID` | Sí | Único chat autorizado para usar el bot. |
| `GROQ_API_KEY` | Sí | Proveedor principal de clasificación de ofertas (nivel gratuito, sin tarjeta de crédito; `console.groq.com`). |
| `OPENROUTER_API_KEY` | No | Respaldo de clasificación si Groq falla del todo (`openrouter.ai`, tampoco requiere tarjeta). Sin ella, el respaldo se omite (log de aviso) y la clasificación depende solo de Groq. |
| `GIT_PUSH_TOKEN` | No | GitHub Personal Access Token (fine-grained, permiso *Contents: Read and write* solo sobre este repo) para que `controller.py`/`git_sync.py` puedan pushear `offers.db` desde Render. Sin ella el bot funciona igual, pero las decisiones no se sincronizan con git y se pierden en el siguiente redeploy. |
| `JOOBLE_API_KEY` | No | Búsqueda en el agregador Jooble (cobertura España). Se consigue en `es.jooble.org/api/about` (registro self-service, la key llega por email). Sin ella, `search_jooble` no hace ninguna llamada HTTP y esa fuente queda desactivada (el resto sigue funcionando igual). |
| `MAX_CLASSIFICATIONS_PER_RUN` | No (por defecto 30) | Tope de llamadas a Groq por ejecución del cron, para no agotar la cuota de golpe. |

## Despliegue

- **Búsqueda + clasificación**: `.github/workflows/search.yml`, cron a las
  06:00 y 18:00 UTC (`workflow_dispatch` también disponible para lanzarlo a
  mano). Tiene `concurrency` para que dos ejecuciones no se pisen sobre
  `offers.db`.
- **Bot de Telegram**: servicio web en Render (`render.yaml`), siempre
  encendido, ejecuta `python controller.py`. Redeploy automático en cada push
  a `main`.

## Desarrollo local

```bash
pip install -r requirements.txt
cp .env.example .env   # y rellena las variables
python main.py          # una ejecución de búsqueda + clasificación
python controller.py    # el bot de Telegram (Flask + polling)
```

### Tests

```bash
pytest
```

Cubren el backoff de Groq y el respaldo a OpenRouter, el reparto de
cupo/reintento de errores en `classify_node`, y toda la lógica de
`contacts`/`/seguimiento`/recordatorios. Usan mocks para Groq/OpenRouter y
Telegram — no consumen cuota real ni mandan mensajes al ejecutarlos.

## Decisiones de diseño a tener en cuenta

- **SQLite versionado en git** en vez de una base de datos gestionada: es
  deliberadamente simple para el volumen de este proyecto (decenas de filas),
  no un patrón a copiar a gran escala. Si el repo es público, cualquier dato
  real que se guarde en `contacts` (nombres, contactos de reclutadores) queda
  en el historial de git para siempre, incluso si se borra después — plantéate
  poner el repo en privado antes de guardar contactos reales. El comando
  `/olvidar Empresa X` borra de verdad (DELETE) la fila de `contacts` de esa
  empresa — a diferencia del botón "❌ Cerrar", que solo la marca como
  `closed` y la conserva — pero eso no limpia el historial de git: evita
  anotar en `notes` datos personales del reclutador que no sean
  estrictamente necesarios para el seguimiento.
- **No hay generador de mensajes de seguimiento con IA**: se decidió
  explícitamente no construirlo por ahora (el recordatorio por sí solo ya
  cubre la mayor parte del valor con mucho menos esfuerzo/riesgo).
- **El bot nunca envía nada de forma automática a terceros** — todo lo que
  sale por Telegram es para que el propio estudiante lo revise y actúe.
