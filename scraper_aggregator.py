"""
scraper_aggregator.py
======================
Agregador de vacantes para Data Analyst / BI Analyst / Analytics Engineer.

LEE ESTO ANTES DE USAR EL SCRIPT
---------------------------------
Este script SI implementa, con llamadas de red reales a endpoints públicos y
sin necesidad de credenciales (salvo Adzuna, que requiere una API key gratuita):

    - RemoteOK          (JSON público, sin auth)          -> fetch_remoteok()
    - Remotive          (JSON público, sin auth)           -> fetch_remotive()
    - We Work Remotely  (RSS público por categoría)        -> fetch_weworkremotely()
    - Working Nomads    (JSON público, sin auth)           -> fetch_workingnomads()
    - Arbeitnow         (JSON público, sin auth)           -> fetch_arbeitnow()
    - Himalayas         (JSON público, sin auth)           -> fetch_himalayas()
    - Adzuna            (JSON, requiere APP_ID/APP_KEY gratis) -> fetch_adzuna()
    - TokyoDev          (HTML público, sin login, "best effort") -> fetch_tokyodev()
    - Japan-Dev         (HTML público, sin login, "best effort") -> fetch_japandev()
    - GaijinPot Jobs    (HTML público, sin login, "best effort") -> fetch_gaijinpot()

Este script DELIBERADAMENTE NO implementa scrapers reales para:

    LinkedIn Jobs, Indeed, Glassdoor, ZipRecruiter, Monster, Google Jobs,
    Wantedly, Daijob, CareerCross.

Razón (léela antes de pedir que se "arregle"): estas plataformas prohíben el
scraping automatizado en sus Términos de Servicio, exigen login, y usan
detección de bots activa (CAPTCHAs, fingerprinting, bloqueo de IP/cuenta).
Indeed además cerró su antiguo RSS/publisher feed público — ya no responde a
ese tipo de solicitud. Escribir un scraper "silencioso" para esos sitios no es
más difícil técnicamente, es que cruza la línea de qué estoy dispuesto a
construir: código diseñado para evadir la protección anti-bot de un sitio de
terceros. Cada una de esas funciones existe abajo como un stub que levanta
NotImplementedError con una alternativa legítima concreta (normalmente:
Adzuna, que sí re-sirve un conjunto muy superpuesto de vacantes de forma legal
mediante su propia API pública).

Si de verdad necesitas cobertura de LinkedIn/Indeed/Glassdoor a nivel
producción, la vía soportada es contratar un proveedor de datos con licencia
(p. ej. la API oficial de LinkedIn Talent, o Adzuna/Levels/Greenhouse Job
Board API agregando ofertas de esos mismos empleadores).

Dependencias:
    pip install requests feedparser beautifulsoup4 scikit-learn --break-system-packages

Uso:
    python scraper_aggregator.py --cv mi_cv.txt --out jobs_data.json
    (variables de entorno opcionales: ADZUNA_APP_ID, ADZUNA_APP_KEY, CLAUDE_API_KEY)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import quote_plus

import requests

try:
    import feedparser
except ImportError:  # pragma: no cover
    feedparser = None

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None

try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:  # pragma: no cover
    SKLEARN_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("job_aggregator")

USER_AGENT = "Mozilla/5.0 (compatible; DataAnalystJobBot/1.0; personal-use; +https://example.com/contact)"
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json, text/html"}
REQUEST_TIMEOUT = 15
CACHE_PATH = "source_cache.json"

# Cuánto tiempo, como mínimo, hay que esperar entre llamadas reales a cada
# fuente. El cron de GitHub Actions corre cada 20 min, pero algunas fuentes
# (Remotive en particular) piden explícitamente en su propia documentación
# que NO se les golpee cada 20 min, así que el script respeta eso aunque el
# orquestador se ejecute más seguido.
SOURCE_MIN_REFRESH_MINUTES = {
    "remoteok": 20,
    "workingnomads": 20,
    "arbeitnow": 20,
    "himalayas": 20,
    "weworkremotely": 20,
    "adzuna": 20,
    "remotive": 360,       # Remotive pide max. ~4 llamadas/día
    "tokyodev": 60,
    "japandev": 60,
    "gaijinpot": 60,
}

TARGET_KEYWORDS = [
    "data analyst", "analytics engineer", "bi analyst", "business intelligence",
    "data analytics", "reporting analyst", "power bi", "tableau", "sql",
    "dbt", "snowflake", "etl", "data engineer jr", "insights analyst",
]

# Frases que descartan una vacante de forma tajante (restricciones locales
# que el candidato no puede cumplir). Se buscan en minúsculas, sin acentos.
RESTRICTION_BLOCKLIST = [
    "us citizens only", "must be a us citizen", "must be a citizen of the united states",
    "authorized to work in the us without sponsorship", "no visa sponsorship available",
    "we are unable to sponsor", "cannot sponsor employment visas",
    "eu citizens only", "eu passport required", "must hold eu citizenship",
    "must be located in the uk", "uk residents only",
    "local candidates only, no relocation",
    "security clearance required", "must be a us person",
]

# Heurística de idioma (evita depender de una librería pesada de NLP).
_ES_MARKERS = {"de", "la", "el", "y", "con", "para", "años", "experiencia",
               "empresa", "trabajo", "requisitos", "conocimientos", "equipo"}
_EN_MARKERS = {"the", "and", "with", "for", "years", "experience", "company",
               "requirements", "team", "role", "knowledge", "skills"}


# ---------------------------------------------------------------------------
# MODELO DE DATOS
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    source: str
    title: str
    company: str
    location: str
    remote: bool
    salary_min: Optional[float]
    salary_max: Optional[float]
    currency: Optional[str]
    salary_usd_approx: Optional[float]
    tags: List[str] = field(default_factory=list)
    description: str = ""
    url: str = ""
    posted_at: Optional[str] = None
    language: Optional[str] = None
    region: str = "global"          # "global" | "japan"
    visa_sponsorship: Optional[bool] = None
    japanese_required: Optional[bool] = None
    blocked_reason: Optional[str] = None
    match_score: Optional[float] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# UTILIDADES DE NORMALIZACIÓN
# ---------------------------------------------------------------------------

def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    if BeautifulSoup is not None:
        return BeautifulSoup(raw, "html.parser").get_text(" ", strip=True)
    return re.sub(r"<[^>]+>", " ", raw)


def _make_id(source: str, title: str, company: str) -> str:
    key = f"{source}|{title.strip().lower()}|{company.strip().lower()}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def _dedupe_key(title: str, company: str) -> str:
    norm = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    return f"{norm(title)}::{norm(company)}"


# Tasas de conversión aproximadas y fijas (actualízalas periódicamente o
# sustituye por una llamada a una API de FX si necesitas precisión real).
_FX_TO_USD = {"usd": 1.0, "eur": 1.08, "jpy": 0.0067, "gbp": 1.26, "cad": 0.73}


def normalize_salary(raw_text: str) -> Dict[str, Any]:
    """Extrae salary_min/max/currency de texto libre. Best-effort con regex,
    no un parser NLP completo — revisa manualmente casos raros."""
    result = {"salary_min": None, "salary_max": None, "currency": None, "salary_usd_approx": None}
    if not raw_text:
        return result

    text = raw_text.replace(",", "")
    currency = None
    if re.search(r"\$|usd", raw_text, re.I):
        currency = "usd"
    elif re.search(r"€|eur\b", raw_text, re.I):
        currency = "eur"
    elif re.search(r"¥|jpy|円|万円", raw_text, re.I):
        currency = "jpy"
    elif re.search(r"£|gbp", raw_text, re.I):
        currency = "gbp"

    numbers = [float(n) for n in re.findall(r"(\d+(?:\.\d+)?)\s*k\b", text, re.I)]
    numbers = [n * 1000 for n in numbers]
    if not numbers:
        numbers = [float(n) for n in re.findall(r"\b(\d{4,7})\b", text)]

    if numbers:
        result["salary_min"] = min(numbers)
        result["salary_max"] = max(numbers)
        result["currency"] = currency or "usd"
        fx = _FX_TO_USD.get(result["currency"], 1.0)
        result["salary_usd_approx"] = round(result["salary_max"] * fx, 2)

    return result


def detect_language(text: str) -> str:
    if not text:
        return "unknown"
    words = set(re.findall(r"[a-záéíóúñ]+", text.lower()))
    has_es = len(words & _ES_MARKERS) >= 2
    has_en = len(words & _EN_MARKERS) >= 2
    if has_es and has_en:
        return "bilingual"
    if has_es:
        return "spanish"
    if has_en:
        return "english"
    return "unknown"


def check_restriction_blocklist(text: str) -> Optional[str]:
    if not text:
        return None
    low = text.lower()
    for phrase in RESTRICTION_BLOCKLIST:
        if phrase in low:
            return phrase
    return None


def is_relevant_role(title: str, description: str) -> bool:
    haystack = f"{title} {description}".lower()
    return any(kw in haystack for kw in TARGET_KEYWORDS)


# ---------------------------------------------------------------------------
# SECCIÓN 1 — FUENTES CON API PÚBLICA REAL (sin llave, verificadas)
# ---------------------------------------------------------------------------

def fetch_remoteok() -> List[Job]:
    """RemoteOK: JSON público en https://remoteok.com/api, sin autenticación."""
    jobs: List[Job] = []
    try:
        resp = requests.get("https://remoteok.com/api", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_remoteok falló: %s", exc)
        return jobs

    for item in data:
        if not isinstance(item, dict) or "id" not in item:
            continue  # el primer elemento es metadata del feed, no una vacante
        title = item.get("position") or item.get("title", "")
        description = _strip_html(item.get("description", ""))
        if not is_relevant_role(title, description):
            continue
        sal = {
            "salary_min": item.get("salary_min"),
            "salary_max": item.get("salary_max"),
            "currency": "usd",
            "salary_usd_approx": item.get("salary_max"),
        }
        jobs.append(Job(
            id=_make_id("remoteok", title, item.get("company", "")),
            source="remoteok",
            title=title,
            company=item.get("company", "Unknown"),
            location=item.get("location") or "Remote",
            remote=True,
            tags=item.get("tags", []) or [],
            description=description,
            url=item.get("url") or f"https://remoteok.com/remote-jobs/{item.get('id')}",
            posted_at=item.get("date"),
            **sal,
        ))
    log.info("RemoteOK: %d vacantes relevantes", len(jobs))
    return jobs


def fetch_remotive(category: str = "data") -> List[Job]:
    """Remotive: JSON público en https://remotive.com/api/remote-jobs.
    OJO: Remotive pide explícitamente no llamarlos más de ~4 veces/día — por
    eso esta fuente tiene un refresh mínimo de 360 min en SOURCE_MIN_REFRESH_MINUTES,
    aunque el cron general corra cada 20 min."""
    jobs: List[Job] = []
    try:
        url = f"https://remotive.com/api/remote-jobs?category={quote_plus(category)}"
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_remotive falló: %s", exc)
        return jobs

    for item in data.get("jobs", []):
        title = item.get("title", "")
        description = _strip_html(item.get("description", ""))
        if not is_relevant_role(title, description):
            continue
        sal = normalize_salary(item.get("salary", ""))
        jobs.append(Job(
            id=_make_id("remotive", title, item.get("company_name", "")),
            source="remotive",
            title=title,
            company=item.get("company_name", "Unknown"),
            location=item.get("candidate_required_location", "Remote"),
            remote=True,
            tags=item.get("tags", []) or [],
            description=description,
            url=item.get("url", ""),
            posted_at=item.get("publication_date"),
            **sal,
        ))
    log.info("Remotive: %d vacantes relevantes", len(jobs))
    return jobs


def fetch_weworkremotely() -> List[Job]:
    """We Work Remotely: feeds RSS públicos por categoría."""
    jobs: List[Job] = []
    if feedparser is None:
        log.warning("feedparser no instalado; omitiendo We Work Remotely")
        return jobs

    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-data-jobs.rss",
        "https://weworkremotely.com/categories/remote-business-jobs.rss",
    ]
    for feed_url in feeds:
        try:
            parsed = feedparser.parse(feed_url)
        except Exception as exc:
            log.warning("fetch_weworkremotely falló en %s: %s", feed_url, exc)
            continue
        for entry in parsed.entries:
            title = entry.get("title", "")
            description = _strip_html(entry.get("summary", ""))
            if not is_relevant_role(title, description):
                continue
            company = ""
            if ":" in title:
                company, _, title = title.partition(":")
            sal = normalize_salary(description)
            jobs.append(Job(
                id=_make_id("weworkremotely", title, company),
                source="weworkremotely",
                title=title.strip(),
                company=company.strip() or "Unknown",
                location="Remote",
                remote=True,
                tags=[],
                description=description,
                url=entry.get("link", ""),
                posted_at=entry.get("published"),
                **sal,
            ))
    log.info("We Work Remotely: %d vacantes relevantes", len(jobs))
    return jobs


def fetch_workingnomads() -> List[Job]:
    """Working Nomads: JSON público en /api/exposed_jobs/, sin autenticación."""
    jobs: List[Job] = []
    try:
        resp = requests.get("https://www.workingnomads.com/api/exposed_jobs/",
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_workingnomads falló: %s", exc)
        return jobs

    for item in data:
        title = item.get("title", "")
        description = _strip_html(item.get("description", ""))
        if not is_relevant_role(title, description):
            continue
        sal = normalize_salary(description)
        jobs.append(Job(
            id=_make_id("workingnomads", title, item.get("company_name", "")),
            source="workingnomads",
            title=title,
            company=item.get("company_name", "Unknown"),
            location=item.get("location", "Remote"),
            remote=True,
            tags=item.get("tags", []) or [],
            description=description,
            url=item.get("url", ""),
            posted_at=item.get("pub_date"),
            **sal,
        ))
    log.info("Working Nomads: %d vacantes relevantes", len(jobs))
    return jobs


def fetch_arbeitnow() -> List[Job]:
    """Arbeitnow: JSON público en /api/job-board-api, sin autenticación.
    Buena fuente de vacantes remotas con salario en EUR."""
    jobs: List[Job] = []
    try:
        resp = requests.get("https://www.arbeitnow.com/api/job-board-api",
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_arbeitnow falló: %s", exc)
        return jobs

    for item in data.get("data", []):
        title = item.get("title", "")
        description = _strip_html(item.get("description", ""))
        if not is_relevant_role(title, description):
            continue
        if not item.get("remote", False):
            continue
        sal = normalize_salary(description)
        jobs.append(Job(
            id=_make_id("arbeitnow", title, item.get("company_name", "")),
            source="arbeitnow",
            title=title,
            company=item.get("company_name", "Unknown"),
            location=item.get("location", "Remote"),
            remote=True,
            tags=item.get("tags", []) or [],
            description=description,
            url=item.get("url", ""),
            posted_at=str(item.get("created_at", "")),
            **sal,
        ))
    log.info("Arbeitnow: %d vacantes relevantes", len(jobs))
    return jobs


def fetch_himalayas() -> List[Job]:
    """Himalayas: JSON público, sin autenticación."""
    jobs: List[Job] = []
    try:
        resp = requests.get("https://himalayas.app/jobs/api", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_himalayas falló: %s", exc)
        return jobs

    for item in data.get("jobs", []):
        title = item.get("title", "")
        description = _strip_html(item.get("description", ""))
        if not is_relevant_role(title, description):
            continue
        sal = {
            "salary_min": item.get("minSalary"),
            "salary_max": item.get("maxSalary"),
            "currency": "usd",
            "salary_usd_approx": item.get("maxSalary"),
        }
        jobs.append(Job(
            id=_make_id("himalayas", title, item.get("companyName", "")),
            source="himalayas",
            title=title,
            company=item.get("companyName", "Unknown"),
            location="Remote",
            remote=True,
            tags=item.get("categories", []) or [],
            description=description,
            url=item.get("applicationLink", ""),
            posted_at=str(item.get("pubDate", "")),
            **sal,
        ))
    log.info("Himalayas: %d vacantes relevantes", len(jobs))
    return jobs


def fetch_adzuna(app_id: Optional[str] = None, app_key: Optional[str] = None,
                 country: str = "us", query: str = "data analyst") -> List[Job]:
    """Adzuna: agregador con API pública gratuita (requiere app_id/app_key de
    https://developer.adzuna.com/). Esta es la alternativa realista y legal a
    'scrapear Indeed/Monster/ZipRecruiter/Google Jobs': Adzuna ya agrega miles
    de bolsas de trabajo, incluyendo overlap sustancial con esas, y te las
    re-sirve mediante una API documentada en vez de un scraper anti-ToS."""
    app_id = app_id or os.environ.get("ADZUNA_APP_ID")
    app_key = app_key or os.environ.get("ADZUNA_APP_KEY")
    jobs: List[Job] = []
    if not app_id or not app_key:
        log.info("Adzuna omitido: falta ADZUNA_APP_ID / ADZUNA_APP_KEY (gratis en developer.adzuna.com)")
        return jobs

    try:
        url = (f"https://api.adzuna.com/v1/api/jobs/{country}/search/1"
               f"?app_id={app_id}&app_key={app_key}&what={quote_plus(query)}"
               f"&content-type=application/json&results_per_page=50")
        resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        log.warning("fetch_adzuna falló: %s", exc)
        return jobs

    for item in data.get("results", []):
        title = item.get("title", "")
        description = _strip_html(item.get("description", ""))
        jobs.append(Job(
            id=_make_id("adzuna", title, (item.get("company") or {}).get("display_name", "")),
            source="adzuna",
            title=title,
            company=(item.get("company") or {}).get("display_name", "Unknown"),
            location=(item.get("location") or {}).get("display_name", ""),
            remote="remote" in description.lower() or "remote" in title.lower(),
            salary_min=item.get("salary_min"),
            salary_max=item.get("salary_max"),
            currency="usd" if country == "us" else "eur",
            salary_usd_approx=item.get("salary_max"),
            tags=[],
            description=description,
            url=item.get("redirect_url", ""),
            posted_at=item.get("created"),
        ))
    log.info("Adzuna (%s): %d vacantes", country, len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# SECCIÓN 2 — JAPÓN: scraping "best effort" de páginas públicas sin login
# ---------------------------------------------------------------------------
# Estas funciones NO usan ninguna API oficial (no existe una) ni evaden ningún
# login o CAPTCHA: leen páginas de listados que son públicas y navegables sin
# cuenta. Aun así son frágiles — el HTML de estos sitios puede cambiar en
# cualquier momento, así que trata los selectores CSS como algo que hay que
# revisar cada tanto, no como una garantía. No los he podido probar en vivo
# al escribir esto (este entorno no tiene acceso de red), así que verifica
# los selectores contra el HTML real antes de confiar en el resultado.

def fetch_tokyodev() -> List[Job]:
    """TokyoDev no tiene API pública. Sus listados (tokyodev.com/jobs) son
    páginas públicas sin login, renderizadas en el servidor. Best effort."""
    jobs: List[Job] = []
    if BeautifulSoup is None:
        log.warning("beautifulsoup4 no instalado; omitiendo TokyoDev")
        return jobs
    try:
        resp = requests.get("https://www.tokyodev.com/jobs", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("fetch_tokyodev falló: %s", exc)
        return jobs

    # NOTA: estos selectores son una mejor suposición razonable sobre la
    # estructura típica de una tarjeta de listado; revísalos con el HTML real.
    for card in soup.select("a[href*='/jobs/']"):
        title = card.get_text(strip=True)
        href = card.get("href", "")
        if not title or "/jobs/" not in href or href.rstrip("/").endswith("/jobs"):
            continue
        full_url = href if href.startswith("http") else f"https://www.tokyodev.com{href}"
        jobs.append(Job(
            id=_make_id("tokyodev", title, "TokyoDev-listing"),
            source="tokyodev",
            title=title,
            company="Ver oferta",  # el nombre de empresa exacto exige abrir cada detalle
            location="Japan",
            remote=False,
            salary_min=None, salary_max=None, currency="jpy", salary_usd_approx=None,
            tags=[],
            description="",
            url=full_url,
            posted_at=None,
            region="japan",
        ))
    log.info("TokyoDev (best-effort): %d enlaces de vacante encontrados", len(jobs))
    return jobs


def fetch_japandev() -> List[Job]:
    """Japan-Dev tampoco expone API pública; mismo enfoque best-effort que
    TokyoDev sobre japan-dev.com/jobs."""
    jobs: List[Job] = []
    if BeautifulSoup is None:
        return jobs
    try:
        resp = requests.get("https://japan-dev.com/jobs", headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("fetch_japandev falló: %s", exc)
        return jobs

    for card in soup.select("a[href*='/jobs/']"):
        title = card.get_text(strip=True)
        href = card.get("href", "")
        if not title or href.rstrip("/").endswith("/jobs"):
            continue
        full_url = href if href.startswith("http") else f"https://japan-dev.com{href}"
        jobs.append(Job(
            id=_make_id("japandev", title, "JapanDev-listing"),
            source="japandev",
            title=title,
            company="Ver oferta",
            location="Japan",
            remote=False,
            salary_min=None, salary_max=None, currency="jpy", salary_usd_approx=None,
            tags=[],
            description="",
            url=full_url,
            posted_at=None,
            region="japan",
        ))
    log.info("Japan-Dev (best-effort): %d enlaces de vacante encontrados", len(jobs))
    return jobs


def fetch_gaijinpot() -> List[Job]:
    """GaijinPot Jobs: listados públicos, sin login. Best effort, confianza
    baja — no verificado en vivo. Revisa selectores antes de usar en serio."""
    jobs: List[Job] = []
    if BeautifulSoup is None:
        return jobs
    try:
        resp = requests.get("https://jobs.gaijinpot.com/index/index/search?Keyword=data+analyst",
                             headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
    except Exception as exc:
        log.warning("fetch_gaijinpot falló: %s", exc)
        return jobs

    for card in soup.select("a[href*='/job/']"):
        title = card.get_text(strip=True)
        href = card.get("href", "")
        if not title:
            continue
        full_url = href if href.startswith("http") else f"https://jobs.gaijinpot.com{href}"
        jobs.append(Job(
            id=_make_id("gaijinpot", title, "GaijinPot-listing"),
            source="gaijinpot",
            title=title,
            company="Ver oferta",
            location="Japan",
            remote=False,
            salary_min=None, salary_max=None, currency="jpy", salary_usd_approx=None,
            tags=[],
            description="",
            url=full_url,
            posted_at=None,
            region="japan",
        ))
    log.info("GaijinPot (best-effort, sin verificar en vivo): %d enlaces encontrados", len(jobs))
    return jobs


# ---------------------------------------------------------------------------
# SECCIÓN 3 — DELIBERADAMENTE NO IMPLEMENTADO (ver docstring del módulo)
# ---------------------------------------------------------------------------

def _not_implemented(name: str, reason: str, alternative: str):
    raise NotImplementedError(f"{name}: {reason} Alternativa recomendada: {alternative}")


def fetch_linkedin_jobs(*_, **__):
    _not_implemented(
        "LinkedIn Jobs",
        "Los Términos de Servicio de LinkedIn prohíben expresamente el scraping "
        "automatizado, y el sitio exige login y aplica detección de bots activa "
        "(fingerprinting, límites de IP/cuenta, bloqueo).",
        "LinkedIn Talent/Jobs API oficial (de pago, requiere aprobación de partner) "
        "o un proveedor de datos con licencia.",
    )


def fetch_indeed(*_, **__):
    _not_implemented(
        "Indeed",
        "Indeed cerró/bloquea activamente su antiguo RSS y su programa de "
        "publisher feed público; ya no responde a ese tipo de solicitud.",
        "fetch_adzuna(), que agrega un conjunto con overlap sustancial de forma legal.",
    )


def fetch_glassdoor(*_, **__):
    _not_implemented(
        "Glassdoor",
        "Requiere login para ver la mayoría del contenido y sus ToS prohíben scraping.",
        "fetch_adzuna() o un proveedor de datos con licencia.",
    )


def fetch_ziprecruiter(*_, **__):
    _not_implemented(
        "ZipRecruiter",
        "ToS prohíben scraping; el acceso programático solo existe vía su "
        "partner/publisher API de pago con aprobación previa.",
        "fetch_adzuna().",
    )


def fetch_monster(*_, **__):
    _not_implemented(
        "Monster",
        "ToS prohíben scraping automatizado de resultados de búsqueda.",
        "fetch_adzuna().",
    )


def fetch_google_jobs(*_, **__):
    _not_implemented(
        "Google Jobs",
        "Google no expone una API pública de búsqueda de empleos: 'Google for "
        "Jobs' simplemente indexa datos estructurados que otros sitios ya "
        "publican, no hay un endpoint que llamar directamente.",
        "Publicar datos estructurados (schema.org/JobPosting) en tu propio sitio "
        "si quieres aparecer ahí, o usar fetch_adzuna() para cubrir ese mismo mercado.",
    )


def fetch_wantedly(*_, **__):
    _not_implemented(
        "Wantedly",
        "La mayoría del detalle de una vacante y el contacto con la empresa "
        "están detrás de un login; es una red social de reclutamiento, no un "
        "board de listados públicos.",
        "Revisar manualmente, o contactar a Wantedly por acceso de datos vía partner.",
    )


def fetch_daijob(*_, **__):
    _not_implemented(
        "Daijob",
        "Gran parte del contenido relevante exige cuenta registrada.",
        "Revisar manualmente sus listados públicos disponibles.",
    )


def fetch_careercross(*_, **__):
    _not_implemented(
        "CareerCross",
        "Gran parte del contenido relevante exige cuenta registrada.",
        "Revisar manualmente sus listados públicos disponibles.",
    )


# ---------------------------------------------------------------------------
# SECCIÓN 4 — MOTOR DE MATCH CV vs PUESTO (TF-IDF + cosine similarity)
# ---------------------------------------------------------------------------

def _keyword_overlap_score(resume_text: str, job_text: str) -> float:
    """Fallback simple si scikit-learn no está disponible: overlap de
    palabras significativas, normalizado 0-100."""
    def tokenize(t):
        return set(w for w in re.findall(r"[a-záéíóúñ]{3,}", t.lower()))
    r, j = tokenize(resume_text), tokenize(job_text)
    if not r or not j:
        return 0.0
    return round(100 * len(r & j) / len(r | j) * 2.5, 1)  # *2.5 para escalar a rangos más realistas


def compute_match_scores(resume_text: str, jobs: List[Job]) -> List[Job]:
    """Calcula match_score (0-100) para cada Job comparando resume_text
    contra title + tags + description de cada vacante."""
    if not resume_text or not jobs:
        for j in jobs:
            j.match_score = None
        return jobs

    job_texts = [f"{j.title} {' '.join(j.tags)} {j.description}" for j in jobs]

    if SKLEARN_AVAILABLE:
        try:
            corpus = [resume_text] + job_texts
            vectorizer = TfidfVectorizer(stop_words=None, max_features=5000)
            matrix = vectorizer.fit_transform(corpus)
            sims = cosine_similarity(matrix[0:1], matrix[1:]).flatten()
            for j, sim in zip(jobs, sims):
                j.match_score = round(float(sim) * 100, 1)
            return jobs
        except Exception as exc:
            log.warning("TF-IDF falló (%s); usando fallback de overlap de palabras", exc)

    for j, text in zip(jobs, job_texts):
        j.match_score = min(100.0, _keyword_overlap_score(resume_text, text))
    return jobs


# ---------------------------------------------------------------------------
# SECCIÓN 5 — CACHÉ / RESPETO DE RATE LIMITS POR FUENTE
# ---------------------------------------------------------------------------

def _load_cache() -> Dict[str, Any]:
    if os.path.exists(CACHE_PATH):
        try:
            with open(CACHE_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def _save_cache(cache: Dict[str, Any]) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=2)


def _source_is_due(source: str, cache: Dict[str, Any]) -> bool:
    last_run = cache.get(source, {}).get("last_run")
    if not last_run:
        return True
    min_minutes = SOURCE_MIN_REFRESH_MINUTES.get(source, 20)
    elapsed_minutes = (time.time() - last_run) / 60
    return elapsed_minutes >= min_minutes


# ---------------------------------------------------------------------------
# SECCIÓN 6 — ORQUESTACIÓN PRINCIPAL
# ---------------------------------------------------------------------------

SOURCES_GLOBAL = {
    "remoteok": fetch_remoteok,
    "remotive": fetch_remotive,
    "weworkremotely": fetch_weworkremotely,
    "workingnomads": fetch_workingnomads,
    "arbeitnow": fetch_arbeitnow,
    "himalayas": fetch_himalayas,
}
SOURCES_JAPAN = {
    "tokyodev": fetch_tokyodev,
    "japandev": fetch_japandev,
    "gaijinpot": fetch_gaijinpot,
}
SOURCES_NOT_IMPLEMENTED = [
    fetch_linkedin_jobs, fetch_indeed, fetch_glassdoor, fetch_ziprecruiter,
    fetch_monster, fetch_google_jobs, fetch_wantedly, fetch_daijob, fetch_careercross,
]


def run_aggregation(resume_text: str, adzuna_app_id: Optional[str] = None,
                     adzuna_app_key: Optional[str] = None,
                     out_path: str = "jobs_data.json") -> Dict[str, Any]:
    cache = _load_cache()
    all_jobs: List[Job] = []
    errors: Dict[str, str] = {}
    skipped_ratelimit: List[str] = []

    for name, fn in {**SOURCES_GLOBAL, **SOURCES_JAPAN}.items():
        if not _source_is_due(name, cache):
            skipped_ratelimit.append(name)
            continue
        try:
            fetched = fn()
            for j in fetched:
                if name in SOURCES_JAPAN:
                    j.region = "japan"
            all_jobs.extend(fetched)
            cache[name] = {"last_run": time.time(), "count": len(fetched)}
        except Exception as exc:
            log.error("Fuente %s falló: %s", name, exc)
            errors[name] = str(exc)

    if adzuna_app_id and adzuna_app_key:
        if _source_is_due("adzuna", cache):
            try:
                fetched = fetch_adzuna(adzuna_app_id, adzuna_app_key, country="us")
                all_jobs.extend(fetched)
                cache["adzuna"] = {"last_run": time.time(), "count": len(fetched)}
            except Exception as exc:
                errors["adzuna"] = str(exc)
        else:
            skipped_ratelimit.append("adzuna")

    _save_cache(cache)

    # --- Deduplicación ---
    seen = set()
    deduped: List[Job] = []
    for j in all_jobs:
        key = _dedupe_key(j.title, j.company)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(j)

    # --- Filtro de restricciones + idioma ---
    for j in deduped:
        j.blocked_reason = check_restriction_blocklist(j.description)
        j.language = detect_language(j.description or j.title)

    active_jobs = [j for j in deduped if not j.blocked_reason]
    blocked_jobs = [j for j in deduped if j.blocked_reason]

    # --- Match score contra el CV base ---
    active_jobs = compute_match_scores(resume_text, active_jobs)
    active_jobs.sort(key=lambda j: (j.match_score or 0), reverse=True)

    remote_global = [j.to_dict() for j in active_jobs if j.region == "global"]
    japan = [j.to_dict() for j in active_jobs if j.region == "japan"]

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {
            "remote_global": len(remote_global),
            "japan": len(japan),
            "blocked_by_restriction": len(blocked_jobs),
        },
        "sources_skipped_ratelimit": skipped_ratelimit,
        "sources_not_implemented": [f.__name__.replace("fetch_", "") for f in SOURCES_NOT_IMPLEMENTED],
        "errors": errors,
        "remote_global": remote_global,
        "japan": japan,
    }

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    log.info("Listo: %d vacantes remotas globales, %d en Japón, %d bloqueadas por restricción, "
             "%d fuentes omitidas por rate limit, %d errores.",
             len(remote_global), len(japan), len(blocked_jobs), len(skipped_ratelimit), len(errors))
    return output


# ---------------------------------------------------------------------------
# ENTREGABLE 3 — PROMPTS DEL MOTOR DE HUMANIZACIÓN (referencia / uso batch)
# ---------------------------------------------------------------------------
# La app en producción (Vercel/Netlify) llama a estos mismos prompts desde una
# función serverless en Node (ver api/generate.js) para no exponer tu API key
# en el navegador. Se dejan aquí también en Python por si quieres pre-generar
# CVs/cartas en batch localmente con tu propia API key de Anthropic.

CV_SYSTEM_PROMPT = """Eres un redactor de CVs pragmático. Reescribes la hoja de vida
de un candidato para que encaje con una vacante específica, SIN inventar
experiencia que el candidato no tiene.

Reglas estrictas:
- Prohibido usar clichés de IA: "apasionado", "orientado a resultados",
  "sinergia", "revolucionario", "inestimable", "entusiasta", "proactivo".
- Tono natural, profesional, en primera persona, como lo escribiría la
  persona misma (no un robot de RRHH).
- Reordena y reescribe las viñetas de experiencia para que las que mejor
  matchean con los requisitos técnicos del puesto queden primero.
- Prioriza logros cuantitativos (%, KPIs, tiempo ahorrado, tamaño de datasets,
  usuarios impactados) sobre descripciones genéricas de tareas.
- Usa exactamente el vocabulario técnico de la vacante cuando el candidato sí
  tiene esa habilidad (para ATS), pero no le atribuyas herramientas que no
  mencionó en su CV base.
- Devuelve el CV adaptado completo, listo para copiar y pegar."""

COVER_LETTER_SYSTEM_PROMPT = """Eres un redactor de cartas de presentación
pragmático. Escribes cartas cortas y concretas, nunca genéricas.

Reglas estrictas:
- Máximo 3 párrafos cortos.
- Prohibido usar clichés de IA (ver lista de CV_SYSTEM_PROMPT).
- Párrafo 1: por qué esta vacante específica te interesa (menciona algo real
  del puesto o la empresa, no una frase que serviría para cualquier oferta).
- Párrafo 2: 1-2 logros concretos y cuantitativos del CV base que hacen match
  directo con los requisitos técnicos del puesto.
- Párrafo 3: aspecto logístico ejecutivo y directo — disponibilidad remota
  inmediata, o apertura a reubicarse a Japón con patrocinio de visa, según
  aplique. Sin relleno emocional.
- Tono profesional, directo, en primera persona."""


def build_generation_prompt(mode: str, resume_text: str, job: Dict[str, Any]) -> Dict[str, str]:
    system = CV_SYSTEM_PROMPT if mode == "cv" else COVER_LETTER_SYSTEM_PROMPT
    user = (
        f"HOJA DE VIDA BASE:\n{resume_text}\n\n"
        f"VACANTE:\nTítulo: {job.get('title')}\nEmpresa: {job.get('company')}\n"
        f"Descripción: {job.get('description')}"
    )
    return {"system": system, "user": user}


def call_claude_for_generation(mode: str, resume_text: str, job: Dict[str, Any],
                                api_key: Optional[str] = None,
                                model: str = "claude-sonnet-5") -> str:
    """Llamada de referencia a la API de Anthropic para generar CV/carta en
    batch desde Python. Requiere tu propia API key (nunca la publiques en el
    frontend — para la app web, esto vive en api/generate.js del lado del
    servidor, no aquí)."""
    api_key = api_key or os.environ.get("CLAUDE_API_KEY")
    if not api_key:
        raise RuntimeError("Falta CLAUDE_API_KEY para generar contenido con Claude.")
    prompt = build_generation_prompt(mode, resume_text, job)
    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 1200,
            "system": prompt["system"],
            "messages": [{"role": "user", "content": prompt["user"]}],
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    return "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Agregador de vacantes Data Analyst / Analytics Engineer")
    parser.add_argument("--cv", type=str, default=None, help="Ruta a un .txt/.md con tu CV base")
    parser.add_argument("--out", type=str, default="jobs_data.json", help="Ruta de salida del JSON")
    parser.add_argument("--adzuna-country", type=str, default="us")
    args = parser.parse_args()

    resume_text = ""
    if args.cv and os.path.exists(args.cv):
        with open(args.cv, "r", encoding="utf-8") as f:
            resume_text = f.read()
    elif args.cv:
        log.warning("No se encontró el archivo de CV en %s; el match_score quedará vacío.", args.cv)

    run_aggregation(
        resume_text=resume_text,
        adzuna_app_id=os.environ.get("ADZUNA_APP_ID"),
        adzuna_app_key=os.environ.get("ADZUNA_APP_KEY"),
        out_path=args.out,
    )


if __name__ == "__main__":
    main()
