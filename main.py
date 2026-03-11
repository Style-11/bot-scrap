"""
Bot Cazador de Cursos Top y Becas de Tecnología
Playwright + Stealth · Telegram Alerts · Railway Deploy
"""

import asyncio
import json
import os
import re
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote_plus, urljoin

import httpx
from dotenv import load_dotenv
from playwright.async_api import async_playwright

# ─── Configuración ────────────────────────────────────────────────────────────

load_dotenv()  # carga .env automáticamente

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("bot-cursos")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

DATA_FILE = Path(__file__).parent / "data.json"

# ─── Umbrales de calidad ("Tridente de Oro") ─────────────────────────────────

MIN_RATING = 4.6
MIN_STUDENTS = 5_000
MAX_MONTHS_OLD = 6
MAX_PRICE_PEN = 39.90

# ─── Listas de búsqueda ──────────────────────────────────────────────────────

INSTRUCTORES_FIJOS = [
    "Fernando Herrera",
    "Maximilian Schwarzmüller",
    "Jonas Schmedtmann",
    "Andrei Neagoie",
    "Stephen Grider",
    "Colt Steele",
]

TENDENCIAS = [
    "Python 2026",
    "IA Agentes",
    "Rust",
    "Ciberseguridad",
    "Arquitectura de Software",
]

BECAS_FUENTES = [
    {
        "nombre": "Oracle ONE",
        "url": "https://www.oracle.com/education/oracle-next-education/",
        "selectores": ["a[href*='scholarship']", "a[href*='beca']", ".cta", "a[href*='register']"],
    },
    {
        "nombre": "Becas Santander",
        "url": "https://www.becas-santander.com/es/index.html",
        "selectores": ["a[href*='technology']", "a[href*='tecnolog']", ".card", ".scholarship-card"],
    },
    {
        "nombre": "Google Career Certificates",
        "url": "https://grow.google/intl/es/certificates/",
        "selectores": [".course-card", "a[href*='certificate']", "a[href*='enroll']"],
    },
    {
        "nombre": "Microsoft Learn",
        "url": "https://learn.microsoft.com/es-es/training/",
        "selectores": ["a[href*='free']", "a[href*='certification']", ".card-content"],
    },
]

PLATAFORMAS_GRATIS = [
    {
        "nombre": "edX - Cursos Gratis",
        "url": "https://www.edx.org/search?tab=course&price=Free",
        "selectores": [".discovery-card", "a[href*='/course/']"],
    },
    {
        "nombre": "Coursera - Gratis",
        "url": "https://www.coursera.org/courses?query=free+technology&sortBy=BEST_MATCH",
        "selectores": [".css-1cj5q1e", "a[href*='/learn/']"],
    },
]


# ─── Persistencia (data.json anti-spam) ───────────────────────────────────────

def cargar_datos() -> dict:
    if DATA_FILE.exists():
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"cursos_notificados": [], "becas_notificadas": [], "ultima_ejecucion": None}


def guardar_datos(datos: dict):
    datos["ultima_ejecucion"] = datetime.utcnow().isoformat()
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(datos, f, ensure_ascii=False, indent=2)


def ya_notificado(datos: dict, identificador: str) -> bool:
    return identificador in datos["cursos_notificados"] or identificador in datos["becas_notificadas"]


def registrar_curso(datos: dict, identificador: str):
    if identificador not in datos["cursos_notificados"]:
        datos["cursos_notificados"].append(identificador)


def registrar_beca(datos: dict, identificador: str):
    if identificador not in datos["becas_notificadas"]:
        datos["becas_notificadas"].append(identificador)


# ─── Telegram ─────────────────────────────────────────────────────────────────

async def notificar_telegram(mensaje: str):
    """Envía la alerta detallada a Telegram."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.warning("TELEGRAM_TOKEN o TELEGRAM_CHAT_ID no configurados. Mensaje:\n%s", mensaje)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": mensaje,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            resp = await client.post(url, json=payload)
            if resp.status_code != 200:
                log.error("Telegram respondió %s: %s", resp.status_code, resp.text)
            else:
                log.info("Alerta Telegram enviada correctamente.")
        except httpx.HTTPError as exc:
            log.error("Error enviando a Telegram: %s", exc)


def formato_curso(curso: dict) -> str:
    """Formato de alerta para un curso encontrado."""
    return (
        "🌟 <b>¡JOYITA ENCONTRADA!</b>\n"
        f"📚 <b>Curso:</b> {curso['nombre']}\n"
        f"👨‍🏫 <b>Instructor:</b> {curso.get('instructor', 'N/A')}\n"
        f"📈 <b>Nivel:</b> {curso.get('nivel', 'N/A')}\n"
        f"⭐ <b>Calidad:</b> {curso['rating']} estrellas de {curso['estudiantes']:,} alumnos\n"
        f"🔄 <b>Última actualización:</b> {curso.get('actualizado', 'N/A')}\n"
        f"💰 <b>Precio:</b> S/ {curso['precio']:.2f} (Oferta detectada)\n"
        f"🔗 <b>Enlace:</b> {curso['url']}"
    )


def formato_beca(beca: dict) -> str:
    """Formato de alerta para una beca / recurso gratuito."""
    return (
        "🎓 <b>¡BECA / RECURSO GRATIS DETECTADO!</b>\n"
        f"🏫 <b>Fuente:</b> {beca['fuente']}\n"
        f"📝 <b>Título:</b> {beca['titulo']}\n"
        f"🔗 <b>Enlace:</b> {beca['url']}"
    )


# ─── Helpers de scraping ─────────────────────────────────────────────────────

def parsear_numero(texto: str) -> float:
    """Extrae un número de un texto como '4.7', '12,345' → 12345."""
    texto = texto.replace(",", "").replace(".", "", texto.count(".") - 1)
    nums = re.findall(r"[\d.]+", texto)
    return float(nums[0]) if nums else 0.0


def parsear_estudiantes(texto: str) -> int:
    texto = texto.replace(",", "").replace(".", "")
    nums = re.findall(r"\d+", texto)
    return int(nums[0]) if nums else 0


def fecha_reciente(texto_fecha: str) -> bool:
    """Verifica si la fecha de actualización es menor a MAX_MONTHS_OLD meses."""
    meses_es = {
        "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
        "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
        "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    }
    meses_en = {
        "january": 1, "february": 2, "march": 3, "april": 4,
        "may": 5, "june": 6, "july": 7, "august": 8,
        "september": 9, "october": 10, "november": 11, "december": 12,
    }
    texto_lower = texto_fecha.lower()
    mes = None
    anio = None
    for nombre, num in {**meses_es, **meses_en}.items():
        if nombre in texto_lower:
            mes = num
            break
    anio_match = re.search(r"20\d{2}", texto_lower)
    if anio_match:
        anio = int(anio_match.group())
    if mes and anio:
        fecha_curso = datetime(anio, mes, 1)
        limite = datetime.utcnow() - timedelta(days=MAX_MONTHS_OLD * 30)
        return fecha_curso >= limite
    return False


# ─── Validación de calidad ("Tridente de Oro") ───────────────────────────────

def validar_calidad_superior(curso: dict) -> bool:
    """Cruza datos de alumnos, estrellas y fecha de actualización."""
    if curso["rating"] < MIN_RATING:
        return False
    if curso["estudiantes"] < MIN_STUDENTS:
        return False
    if curso["precio"] > MAX_PRICE_PEN:
        return False
    if not fecha_reciente(curso.get("actualizado", "")):
        return False
    return True


# ─── Módulo Udemy ─────────────────────────────────────────────────────────────

async def _buscar_udemy(page, termino: str) -> list[dict]:
    """Busca un término en Udemy y recopila los cursos de la primera página."""
    cursos = []
    url = f"https://www.udemy.com/courses/search/?q={quote_plus(termino)}&sort=relevance&lang=es&lang=en"
    log.info("Buscando en Udemy: %s", termino)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)

        # Scroll para cargar lazy-loaded cards
        for _ in range(3):
            await page.evaluate("window.scrollBy(0, 800)")
            await page.wait_for_timeout(1_500)

        cards = await page.query_selector_all("[data-purpose='course-title-url']")
        if not cards:
            cards = await page.query_selector_all("a[href*='/course/']")

        for card in cards[:15]:
            try:
                href = await card.get_attribute("href") or ""
                if not href.startswith("http"):
                    href = "https://www.udemy.com" + href

                titulo_el = await card.query_selector("h3, span, div[data-purpose='course-title-url'] span")
                titulo = (await titulo_el.inner_text()).strip() if titulo_el else "Sin título"

                parent = await card.evaluate_handle("el => el.closest('[class*=\"course-card\"]') || el.parentElement.parentElement.parentElement")

                rating_el = await parent.query_selector("[data-purpose='rating-number'], [class*='star-rating'], span[class*='rating']")
                rating_text = (await rating_el.inner_text()).strip() if rating_el else "0"
                rating = parsear_numero(rating_text)

                students_el = await parent.query_selector("[data-purpose='enrollment-number'], span[class*='enrollment']")
                students_text = (await students_el.inner_text()).strip() if students_el else "0"
                estudiantes = parsear_estudiantes(students_text)

                price_el = await parent.query_selector("[data-purpose='course-price-text'] span span, [data-purpose='course-price-text']")
                price_text = (await price_el.inner_text()).strip() if price_el else "0"
                precio = parsear_numero(price_text)

                level_el = await parent.query_selector("[data-purpose='course-meta-info'] span, span[class*='level']")
                nivel = (await level_el.inner_text()).strip() if level_el else "N/A"

                instructor_el = await parent.query_selector("[data-purpose='safely-set-inner-html:course-card:visible-instructors'], span[class*='instructor']")
                instructor = (await instructor_el.inner_text()).strip() if instructor_el else "N/A"

                updated_el = await parent.query_selector("[data-purpose='last-update-date'], span[class*='updated']")
                actualizado = (await updated_el.inner_text()).strip() if updated_el else ""

                cursos.append({
                    "nombre": titulo,
                    "instructor": instructor,
                    "rating": rating,
                    "estudiantes": estudiantes,
                    "precio": precio,
                    "nivel": nivel,
                    "actualizado": actualizado,
                    "url": href,
                    "termino": termino,
                })
            except Exception as exc:
                log.debug("Error parseando card: %s", exc)
                continue

    except Exception as exc:
        log.error("Error buscando '%s' en Udemy: %s", termino, exc)

    return cursos


async def escanear_tendencias_udemy(page, datos: dict) -> list[dict]:
    """Busca cursos nuevos con alta calificación (instructores fijos + tendencias)."""
    todas_las_busquedas = INSTRUCTORES_FIJOS + TENDENCIAS
    cursos_aprobados = []

    for termino in todas_las_busquedas:
        cursos = await _buscar_udemy(page, termino)
        for curso in cursos:
            identificador = curso["url"].split("?")[0]
            if ya_notificado(datos, identificador):
                continue
            if validar_calidad_superior(curso):
                cursos_aprobados.append(curso)
                registrar_curso(datos, identificador)
                log.info("✅ Curso aprobado: %s (%.1f⭐, %d alumnos, S/%.2f)",
                         curso["nombre"], curso["rating"], curso["estudiantes"], curso["precio"])
        # Pausa entre búsquedas para no saturar
        await page.wait_for_timeout(3_000)

    return cursos_aprobados


# ─── Módulo de Becas y Plataformas Gratis ─────────────────────────────────────

async def _escanear_pagina_becas(page, fuente: dict, datos: dict) -> list[dict]:
    """Escanea una página de becas/recursos gratuitos."""
    resultados = []
    log.info("Escaneando becas: %s", fuente["nombre"])
    try:
        await page.goto(fuente["url"], wait_until="domcontentloaded", timeout=60_000)
        await page.wait_for_timeout(4_000)

        for selector in fuente["selectores"]:
            try:
                elements = await page.query_selector_all(selector)
                for el in elements[:10]:
                    href = await el.get_attribute("href") or ""
                    texto = (await el.inner_text()).strip()[:200]
                    if not href:
                        link_child = await el.query_selector("a")
                        if link_child:
                            href = await link_child.get_attribute("href") or ""

                    if not href or len(texto) < 5:
                        continue

                    if not href.startswith("http"):
                        href = urljoin(fuente["url"], href)

                    # Filtro por palabras relevantes
                    lower = texto.lower() + " " + href.lower()
                    keywords = ["free", "gratis", "beca", "scholarship", "voucher",
                                "100%", "certificate", "certificado", "technology",
                                "tecnolog", "developer", "software", "data", "cloud",
                                "python", "java", "ia", "ai", "cyber"]
                    if not any(kw in lower for kw in keywords):
                        continue

                    identificador = href.split("?")[0]
                    if ya_notificado(datos, identificador):
                        continue

                    resultados.append({
                        "fuente": fuente["nombre"],
                        "titulo": texto[:150],
                        "url": href,
                    })
                    registrar_beca(datos, identificador)
            except Exception:
                continue

    except Exception as exc:
        log.error("Error escaneando %s: %s", fuente["nombre"], exc)

    return resultados


async def rastrear_becas_oficiales(page, datos: dict) -> list[dict]:
    """Revisa los portales de grandes tecnológicas y plataformas gratuitas."""
    todas_las_fuentes = BECAS_FUENTES + PLATAFORMAS_GRATIS
    becas_encontradas = []

    for fuente in todas_las_fuentes:
        resultados = await _escanear_pagina_becas(page, fuente, datos)
        becas_encontradas.extend(resultados)
        await page.wait_for_timeout(2_000)

    return becas_encontradas


# ─── Orquestador principal ────────────────────────────────────────────────────

async def ejecutar_bot():
    """Ciclo principal del bot."""
    log.info("▶ Iniciando Bot Cazador de Cursos & Becas")
    datos = cargar_datos()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        context = await browser.new_context(
            viewport={"width": 1366, "height": 768},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            ),
            locale="es-PE",
            timezone_id="America/Lima",
        )
        # Stealth: ocultar webdriver
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3]});
            Object.defineProperty(navigator, 'languages', {get: () => ['es-PE', 'es', 'en']});
        """)

        page = await context.new_page()

        # ── 1. Módulo Udemy ──
        cursos = await escanear_tendencias_udemy(page, datos)
        for curso in cursos:
            await notificar_telegram(formato_curso(curso))
            await asyncio.sleep(1)

        # ── 2. Módulo Becas ──
        becas = await rastrear_becas_oficiales(page, datos)
        for beca in becas:
            await notificar_telegram(formato_beca(beca))
            await asyncio.sleep(1)

        await browser.close()

    guardar_datos(datos)

    total = len(cursos) + len(becas)
    if total == 0:
        log.info("Sin novedades hoy. Todo ya ha sido notificado previamente.")
        await notificar_telegram("ℹ️ <b>Sin novedades</b>\nNo se encontraron cursos ni becas nuevas que cumplan los criterios.")
    else:
        log.info("✅ Ejecución terminada. %d cursos + %d becas notificados.", len(cursos), len(becas))

    log.info("▶ Bot finalizado. Próxima ejecución programada.")


# ─── Scheduler (intervalo configurable) ──────────────────────────────────────

async def scheduler():
    """Ejecuta el bot periódicamente (por defecto cada 12 horas)."""
    intervalo_horas = int(os.environ.get("INTERVAL_HOURS", "12"))
    log.info("Scheduler activo. Intervalo: cada %d horas.", intervalo_horas)

    while True:
        try:
            await ejecutar_bot()
        except Exception as exc:
            log.exception("Error crítico en ejecución: %s", exc)
            try:
                await notificar_telegram(f"❌ <b>Error en Bot Cursos:</b>\n<code>{exc}</code>")
            except Exception:
                pass
        log.info("Esperando %d horas hasta la próxima ejecución...", intervalo_horas)
        await asyncio.sleep(intervalo_horas * 3600)


if __name__ == "__main__":
    asyncio.run(scheduler())
