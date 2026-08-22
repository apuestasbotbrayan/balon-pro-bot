import asyncio
import io
import json
import logging
import os
import random
import re
import sqlite3
import string
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

import google.generativeai as genai
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

# HTTP ligero para evadir Cloudflare sin navegador (curl_cffi impersonate Chrome)
try:
    from curl_cffi import requests as curl_requests
    HAS_CURL = True
except ImportError:
    curl_requests = None
    HAS_CURL = False
import requests as req_requests
try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

HEADERS_CHROME = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Sec-Ch-Ua": '"Chromium";v="122", "Not(A:Brand";v="24", "Google Chrome";v="122"',
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": '"Windows"',
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://www.flashscore.co/",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

def _to_mobile_url(url: str) -> str:
    if "flashscore.co/partido/" in url:
        return url.replace("www.flashscore.co/partido", "m.flashscore.co/partido").replace("www.flashscore.co", "m.flashscore.co")
    if "flashscore.com/match/" in url:
        return url.replace("www.flashscore.com", "m.flashscore.com")
    return url

async def fetch_flashscore_text(url: str) -> tuple[str, str, str]:
    """HTTP ligero sin Playwright: retorna (texto_limpio, minute, score). Prueba curl_cffi impersonate + fallback requests + URL móvil."""
    def _fetch(u: str) -> str:
        headers = dict(HEADERS_CHROME)
        # Intentar curl_cffi con impersonate Chrome (mejor TLS fingerprint)
        if HAS_CURL and curl_requests is not None:
            try:
                resp = curl_requests.get(u, headers=headers, impersonate="chrome110", timeout=15000)
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text
            except Exception:
                pass
        # Fallback requests
        try:
            resp = req_requests.get(u, headers=headers, timeout=15000)
            if resp.status_code == 200 and len(resp.text) > 500:
                return resp.text
        except Exception:
            pass
        return ""

    def _extract(html: str) -> tuple[str, str, str]:
        if not html:
            return "", "", ""
        text = ""
        minute = ""
        score = ""
        if HAS_BS4:
            try:
                soup = BeautifulSoup(html, "html.parser")
                # Eliminar scripts/styles
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                # Intentar contenedor específico #detail
                detail = soup.select_one("#detail, .container__detail")
                if detail:
                    text = detail.get_text(separator="\n", strip=True)
                else:
                    text = soup.get_text(separator="\n", strip=True)
            except Exception:
                text = re.sub(r"<[^>]+>", "\n", html)
        else:
            text = re.sub(r"<[^>]+>", "\n", html)
        # Limpiar
        text = re.sub(r"\n{2,}", "\n", text).strip()[:4000]
        # Extraer minuto y marcador del texto limpio
        m = re.search(r"\b\d{1,3}(?:\+\d+)?'\b", text)
        if m:
            minute = m.group(0)
        s = re.search(r"\b\d+\s*[-:]\s*\d+\b", text)
        if s:
            score = s.group(0).replace(" ", "").replace(":", "-")
        return text, minute, score

    # Intentar URL original y luego móvil
    candidates = [url, _to_mobile_url(url)]
    # Para /hoy lista, la principal ya es https://www.flashscore.co/
    for cand in candidates:
        html = await asyncio.to_thread(_fetch, cand)
        txt, minute, score = _extract(html)
        _low = txt.lower()
        has_stats = any(k in _low for k in ["h2h", "historial", "alineación", "alineacion", "árbitro", "arbitro", "estadística", "formation", "lineup", "corners", "goles", "tarjetas", "posesión"])
        if len(txt.strip()) > 400 or has_stats:
            return txt[:3000], minute, score
        if len(txt.strip()) > 150:
            # Guardar como último intento
            last = (txt[:3000], minute, score)
        else:
            last = ("", "", "")
    # Si no hay buen contenido, devolver último intento aunque sea corto (Gemini decidirá)
    try:
        return last
    except Exception:
        return "", "", ""

async def fetch_fotmob_data(url: str) -> tuple[str, str, str]:
    """Extracción universal y robusta de matchId de FotMob y consumo de API v1/v2."""
    def _extract_match_id(u: str) -> str:
        clean_u = u.split("?")[0].split("#")[0]
        m = re.search(r"matchId=([a-zA-Z0-9]+)", u)
        if m:
            return m.group(1)
        parts = clean_u.rstrip("/").split("/")
        for part in reversed(parts):
            if re.match(r"^[a-zA-Z0-9]{5,}$", part) and part.lower() not in ["matches", "match", "fotmob", "es"]:
                return part
        return ""

    def _fetch_api(mid: str) -> dict:
        api_url = f"https://www.fotmob.com/api/matchDetails?matchId={mid}"
        headers = dict(HEADERS_CHROME)
        headers["Referer"] = "https://www.fotmob.com/"
        headers["Accept"] = "application/json, text/plain, */*"
        if HAS_CURL and curl_requests is not None:
            try:
                resp = curl_requests.get(api_url, headers=headers, impersonate="chrome110", timeout=15000)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
        try:
            resp = req_requests.get(api_url, headers=headers, timeout=15000)
            if resp.status_code == 200:
                return resp.json()
        except Exception:
            pass
        return {}

    def _format(data: dict) -> tuple[str, str, str]:
        try:
            general = data.get("general") or {}
            header = data.get("header") or {}
            content = data.get("content") or {}
            
            home_name = ""
            away_name = ""
            teams_hdr = header.get("teams") or []
            if isinstance(teams_hdr, list) and len(teams_hdr) >= 2:
                home_name = teams_hdr[0].get("name", "")
                away_name = teams_hdr[1].get("name", "")
            
            if not home_name:
                home = general.get("homeTeam") or {}
                away = general.get("awayTeam") or {}
                home_name = home.get("name", "")
                away_name = away.get("name", "")

            teams_txt = f"{home_name} vs {away_name}" if home_name and away_name else "Partido FotMob"
            league = general.get("leagueName", "") or header.get("leagueName", "")

            minute = "No iniciado"
            score = "0-0"
            status = header.get("status") or general.get("status") or {}
            
            if isinstance(status, dict):
                is_started = status.get("started", True)
                if not is_started:
                    minute = "No iniciado"
                else:
                    live_time = status.get("liveTime") or status.get("time") or {}
                    if isinstance(live_time, dict):
                        minute = live_time.get("short") or live_time.get("long") or "En juego"
                    elif live_time:
                        minute = str(live_time)
            
            if isinstance(teams_hdr, list) and len(teams_hdr) >= 2:
                hs = teams_hdr[0].get("score")
                aws = teams_hdr[1].get("score")
                if hs is not None and aws is not None:
                    score = f"{hs}-{aws}"

            estado_txt = "Pre-partido" if minute == "No iniciado" else "En vivo"
            parts = [
                f"Partido: {teams_txt}",
                f"Liga: {league}",
                f"Estado: {estado_txt} | Minuto: {minute} | Marcador: {score}",
                f"Contexto: {'Partido aún no iniciado - evaluar alineaciones probables, bajas, H2H y tendencias con normalidad para mercados pre-partido' if estado_txt == 'Pre-partido' else f'Partido EN VIVO en {minute} con marcador {score} - usar minuto/marcador para decidir mercados lógicos de cierre.'}"
            ]

            match_facts = content.get("matchFacts") or {}
            if match_facts:
                parts.append(f"Hechos del partido: {str(match_facts)[:600]}")

            full_text = "\n".join(parts)
            return full_text[:3500], minute, score
        except Exception as e:
            logging.warning(f"Error formateando JSON FotMob: {e}")
            return str(data)[:3500], "", ""

    mid = _extract_match_id(url)
    if not mid:
        return "", "", ""
    data = await asyncio.to_thread(_fetch_api, mid)
    if not data:
        return "", "", ""
    return _format(data)

async def fetch_match_data(url: str) -> tuple[str, str, str]:
    """Brief: Unifica FotMob y Flashscore sin recursión infinita."""
    low = url.lower()
    if "fotmob.com" in low:
        txt, minute, score = await fetch_fotmob_data(url)
        if txt and len(txt.strip()) > 80:
            return txt, minute, score
    # Si es Flashscore o FotMob falló, usar Flashscore HTTP ligero
    return await fetch_flashscore_text(url)

# ==========================================
# 1. CONFIGURACIÓN Y CREDENCIALES (con soporte para Render Env Vars)
# ==========================================
# En Render configura estas 3 como Environment Variables para no exponerlas en GitHub.
# Si no existen, usa los valores por defecto (útil en local).
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8785828541:AAHuZoLPpmwDYXzXl92b_PxMDxJ3jpY0Q6g")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "AQ.Ab8RN6L6QbT8s_T80lfuqrz9ugSoqf3Cgolk5nwWAsJC6PT_gA")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8021280020"))

DB_NAME = "bot_database.db"

genai.configure(api_key=GEMINI_API_KEY)

# === PROMPT PROACTIVO CORREGIDO - SIN INVENTAR CUOTAS + CONCIENCIA EN VIVO ===
SYSTEM_INSTRUCTION = (
    "Actúa como tipster profesional colombiano parcero experto. Cuando recibas datos de un partido (Flashscore/FotMob), "
    "REVISA CON LUPA las alineaciones probables y bajas de jugadores clave, además de árbitro, H2H y tendencias. "
    "TE INYECTARÉ OBLIGATORIAMENTE el MINUTO ACTUAL (ej. 85', 90+2') y el MARCADOR EN VIVO (ej. 3-0) si el partido está en juego; si es programado dirá 'No iniciado'. "
    "SÉ CONSCIENTE DEL MINUTO EXACTO: si minuto >75 o marcador abultado (ej. 3-0, 4-1), PROHIBIDO sugerir mercados pre-partido obsoletos como 'Ambos Anotan' si ya van 3-0, 'Over 2.5' si ya está definido, o 'Gana X' si no queda tiempo. "
    "En esos casos enfoca SOLO mercados lógicos de cierre en vivo (ej. Under restante, córners finales, posesión, tarjetas por desesperación) o DESCARTA y di claramente 'Parcero, este partido ya va a finalizar, no hay valor, mejor pasamos' si no queda valor. "
    "Sé ULTRA RESUMIDO y VISUAL, sin floro. "
    "Saluda breve parcero ('¡Epa, mi hermano!') y presenta de una las 2 o 3 mejores Value Bets SIN INVENTAR CUOTAS FIJAS. "
    "PROHIBIDO poner números de cuota falsos (no escribas 'Cuota 1.65' si no te la dieron). "
    "Formato obligatorio, máximo 1 línea por opción: '🔥 [Mercado]: [por qué en máx 15 palabras] | 📍 Busca en [BetPlay/Wplay/Codere/Zamba]'. "
    "Ejemplo: '¡Epa, mi hermano! Para este partido le veo estas 2 joyitas:\\n"
    "🔥 Everton Más de 4.5 Córners: El rival sufre a balón parado y el Everton bombardeará por bandas. | 📍 Busca en BetPlay\\n"
    "🔥 BTTS - Sí: Duelos directos históricos con goles de ambos lados, bajas defensivas visitantes. | 📍 Busca en Wplay' "
    "En cada opción menciona en cuál casa legal colombiana (BetPlay, Wplay, Codere o Zamba) conviene buscar esa cuota. "
    "Cierra SIEMPRE exactamente con: '¿Cuál te gusta o qué cuota te ofrece tu casa de apuestas para calcular si le apostamos?' "
    "PROHIBIDO párrafos largos o explicar probabilidad implícita."
)

# === EVALUACIÓN DE CUOTA OPTIMIZADA - AL GRANO + CASA COLOMBIANA ===
SYSTEM_INSTRUCTION_CUOTA = (
    "Actúa como tipster colombiano parcero firme y directo. Cuando el usuario te dé una cuota/mercado, "
    "calcula Probabilidad Implícita (1/Cuota) y EV internamente pero NO muestres fórmula larga. "
    "REVISA con lupa alineaciones y bajas si el mercado es de goles/tiros/jugadores. "
    "Responde AL GRANO en MÁXIMO 2 LÍNEAS: "
    "Línea 1: veredicto en una sola frase con emoji: si EV >5% -> '🟢 ¡Métale con confianza! (EV +X%)' "
    "si no -> '🔴 ¡Pilas, no bote la plata por ahí! (EV -X% / Sin valor)'. "
    "Línea 2: justificación técnica ultra corta en 1 frase (máx 20 palabras, pondera árbitro/H2H o bajas si aplica) + menciona casa recomendada: '📍 Búscala en [BetPlay/Wplay/Codere/Zamba]'. "
    "Tono firme, parcero, sin floro ni explicaciones matemáticas aburridas."
)

# === PROMPT PARA COMBINADAS / PARLAYS ===
SYSTEM_INSTRUCTION_COMBINADA = (
    "Actúa como tipster profesional colombiano parcero experto en combinadas/parlays. "
    "Recibes datos de VARIOS partidos de Flashscore para armar una combinada. "
    "REVISA con lupa alineaciones probables y bajas de cada partido, además de árbitro, H2H y tendencias. "
    "Sé ULTRA RESUMIDO y VISUAL, sin floro. "
    "Analiza la viabilidad CONJUNTA: si conviene combinar o si un partido contamina la combinada. "
    "Propón 1 combinada principal de 2-3 selecciones (1 por partido si es posible) en formato: "
    "'🔥 [Partido - Mercado]: [por qué 10 palabras] | 📍 BetPlay/Wplay/Codere/Zamba'. "
    "Si ves mucho riesgo, dilo claro. Sugiere dónde armar el parlay (BetPlay, Wplay, Codere o Zamba) y por qué casa tiene mejores cuotas para ese tipo de mercado. "
    "Cierra con: '¿Cuál te gusta o qué cuota te ofrece tu casa de apuestas para calcular si le apostamos?' "
    "PROHIBIDO inventar cuotas fijas, máximo 1 línea por selección."
)

# ==========================================
# 2. TECLADOS INLINE (UI/UX)
# ==========================================
def kb_proactivo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Ver Historial", callback_data="ver_historial"),
            InlineKeyboardButton(text="💰 Consultar Banca", callback_data="consultar_banca")
        ],
        [
            InlineKeyboardButton(text="⚽ Modo Combinada", callback_data="modo_combinada")
        ]
    ])

def kb_cuota() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📋 Ver mi Historial", callback_data="ver_historial")
        ],
        [
            InlineKeyboardButton(text="📍 Ir a BetPlay", url="https://www.betplay.com.co"),
            InlineKeyboardButton(text="📍 Ir a Wplay", url="https://www.wplay.co")
        ]
    ])

def kb_combinada() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="➕ Agregar otro partido", callback_data="agregar_otro")
        ],
        [
            InlineKeyboardButton(text="🔥 Calcular Parlay", callback_data="calcular_parlay"),
            InlineKeyboardButton(text="❌ Cancelar", callback_data="cancelar_combinada")
        ]
    ])

# ==========================================
# 3. CAPA DE DATOS (SQLite) Y CONTROL DE ACCESO
# ==========================================
def init_db():
    """Crea las tablas: usuarios, licencias, historial_apuestas + migraciones."""
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            telegram_id INTEGER PRIMARY KEY,
            enlaces_hoy INTEGER DEFAULT 0,
            fecha_expiracion TEXT,
            banca_actual REAL DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS licencias (
            codigo TEXT PRIMARY KEY,
            dias_duracion INTEGER,
            usado INTEGER DEFAULT 0
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_apuestas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            partido TEXT,
            mercado TEXT,
            cuota REAL,
            estado TEXT DEFAULT 'PENDIENTE',
            fecha TEXT
        )
    """)
    for col, col_def in [("ultimo_uso", "TEXT"), ("banca_actual", "REAL DEFAULT 0")]:
        try:
            cursor.execute(f"ALTER TABLE usuarios ADD COLUMN {col} {col_def}")
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()

def check_user_access(telegram_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT enlaces_hoy, fecha_expiracion, ultimo_uso FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False, "⛔ Acceso denegado. No estás registrado. Usa /start y proporciona tu código de licencia."
    enlaces_hoy, fecha_expiracion_str, ultimo_uso = row
    today_str = datetime.now().strftime("%Y-%m-%d")
    try:
        exp_date = datetime.strptime(fecha_expiracion_str, "%Y-%m-%d")
        if datetime.now().date() > exp_date.date():
            return False, "❌ Tu licencia ha expirado. Contacta al administrador para renovarla."
    except Exception:
        return False, "❌ Error al verificar la expiración de tu licencia."
    if ultimo_uso != today_str:
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("UPDATE usuarios SET enlaces_hoy = 0, ultimo_uso = ? WHERE telegram_id = ?", (today_str, telegram_id))
        conn.commit()
        conn.close()
        enlaces_hoy = 0
    if enlaces_hoy >= 10:
        return False, "🚫 Has alcanzado el límite diario de 10 enlaces. Vuelve mañana."
    return True, ""

def check_user_valid(telegram_id: int):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT fecha_expiracion FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return False, "⛔ No estás registrado. Usa /start y tu código de licencia."
    try:
        exp_date = datetime.strptime(row[0], "%Y-%m-%d")
        if datetime.now().date() > exp_date.date():
            return False, "❌ Tu licencia ha expirado. Contacta al administrador."
    except Exception:
        return False, "❌ Error al verificar tu licencia."
    return True, ""

def increment_user_usage(telegram_id: int):
    today_str = datetime.now().strftime("%Y-%m-%d")
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET enlaces_hoy = enlaces_hoy + 1, ultimo_uso = ? WHERE telegram_id = ?", (today_str, telegram_id))
    conn.commit()
    conn.close()

def get_banca(telegram_id: int) -> float:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT banca_actual FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row and row[0] is not None:
        try:
            return float(row[0])
        except Exception:
            return 0.0
    return 0.0

def set_banca(telegram_id: int, monto: float):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE usuarios SET banca_actual = ? WHERE telegram_id = ?", (monto, telegram_id))
    conn.commit()
    conn.close()

def format_pesos(valor: float) -> str:
    return f"$ {int(round(valor)):,.0f}".replace(",", ".")

def get_historial_text(telegram_id: int) -> str:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, partido, mercado, cuota, estado, fecha
        FROM historial_apuestas WHERE telegram_id = ? ORDER BY id DESC LIMIT 5
    """, (telegram_id,))
    rows = cursor.fetchall()
    cursor.execute("""
        SELECT COUNT(*), SUM(CASE WHEN estado='ACERTADO' THEN 1 ELSE 0 END), SUM(CASE WHEN estado='FALLIDO' THEN 1 ELSE 0 END), SUM(CASE WHEN estado='PENDIENTE' THEN 1 ELSE 0 END)
        FROM historial_apuestas WHERE telegram_id = ?
    """, (telegram_id,))
    total, acertados, fallidos, pendientes = cursor.fetchone()
    conn.close()
    total = total or 0
    acertados = acertados or 0
    fallidos = fallidos or 0
    pendientes = pendientes or 0
    if total == 0:
        return "📊 *Historial vacío, parcero.*\nAún no has evaluado cuotas. Envíame un Flashscore y luego mándame tu cuota."
    calificados = acertados + fallidos
    efectividad = (acertados / calificados * 100) if calificados > 0 else 0.0
    líneas = []
    for _id, partido, mercado, cuota, estado, fecha in rows:
        emoji = "⏳" if estado == "PENDIENTE" else "🟢" if estado == "ACERTADO" else "🔴"
        estado_txt = "Pendiente" if estado == "PENDIENTE" else "Acertado" if estado == "ACERTADO" else "Fallido"
        partido_corto = (partido[:45] + "…") if len(partido) > 45 else partido
        cuota_txt = f"{cuota:.2f}" if cuota else "—"
        líneas.append(f"{emoji} `#{_id}` {partido_corto}\n   {mercado} @ {cuota_txt} — {estado_txt} ({fecha})")
    historial_txt = "\n\n".join(líneas)
    return (
        f"📊 *Tu Historial (últimas 5)*\n\n"
        f"{historial_txt}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 *Efectividad:* {efectividad:.1f}% ({acertados}✅/{calificados} calificadas)\n"
        f"📦 Total: {total} | ⏳ Pendientes: {pendientes} | 🔴 Fallidas: {fallidos}"
    )

def get_banca_text(telegram_id: int) -> str:
    banca = get_banca(telegram_id)
    if banca > 0:
        return (
            f"💰 Tu banca actual: {format_pesos(banca)}\n"
            f"Stake 2% = {format_pesos(banca*0.02)} | 3% = {format_pesos(banca*0.03)}\n\n"
            f"Para actualizar: `/banca 200000`"
        )
    return "💰 No has configurado banca, parcero.\nUsa `/banca 200000` para que te calcule el stake automático."

# ==========================================
# 4. MÁQUINA DE ESTADOS (FSM)
# ==========================================
class AnalysisStates(StatesGroup):
    waiting_for_license = State()
    waiting_for_link = State()
    waiting_for_quota_or_chat = State()
    waiting_for_quota = State()
    collecting_combinada = State()

router = Router()

async def activate_license(message: Message, codigo: str, state: FSMContext):
    telegram_id = message.from_user.id
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT dias_duracion, usado FROM licencias WHERE codigo = ?", (codigo,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await message.answer("❌ Código inválido. Verifica e intenta de nuevo.")
        return
    dias_duracion, usado = row
    if usado == 1:
        conn.close()
        await message.answer("❌ Este código ya ha sido utilizado.")
        return
    cursor.execute("UPDATE licencias SET usado = 1 WHERE codigo = ?", (codigo,))
    fecha_exp = (datetime.now() + timedelta(days=dias_duracion)).strftime("%Y-%m-%d")
    today_str = datetime.now().strftime("%Y-%m-%d")
    cursor.execute("""
        INSERT OR REPLACE INTO usuarios (telegram_id, enlaces_hoy, fecha_expiracion, ultimo_uso, banca_actual)
        VALUES (?, 0, ?, ?, 0)
    """, (telegram_id, fecha_exp, today_str))
    conn.commit()
    conn.close()
    await state.set_state(AnalysisStates.waiting_for_link)
    await message.answer(
        f"✅ ¡Licencia activada, mi hermano!\n"
        f"📅 Válida por {dias_duracion} días hasta {fecha_exp}.\n\n"
        f"📎 Pilas pues, envíame un enlace de Flashscore y te saco de una las mejores opciones de valor.\n"
        f"💰 Tip: /banca 200000 | 🎯 /combinada | 📊 /historial"
    )

# ==========================================
# 5. HANDLERS - aiogram v3
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    args = message.text.split(maxsplit=1)
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT fecha_expiracion FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        try:
            exp_date = datetime.strptime(row[0], "%Y-%m-%d")
            if datetime.now().date() <= exp_date.date():
                await state.set_state(AnalysisStates.waiting_for_link)
                await message.answer("✅ Licencia activa, parcero. Envíame tu enlace de Flashscore y lo analizamos de una.\n🎯 /combinada | 📊 /historial | 💰 /banca", reply_markup=kb_proactivo())
                return
        except Exception:
            pass
    if len(args) > 1:
        await activate_license(message, args[1].strip(), state)
        return
    await message.answer(
        "👋 ¡Bienvenido al Tipster Bot Pro, mi hermano!\n\n"
        "🔒 Para acceder, envía tu código de licencia.\n"
        "Puedes enviarlo directo o usar:\n"
        "`/activar TU_CODIGO`"
    )
    await state.set_state(AnalysisStates.waiting_for_license)

@router.message(Command("activar"))
async def cmd_activar(message: Message, state: FSMContext):
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Uso: `/activar TU_CODIGO`")
        return
    await activate_license(message, args[1].strip(), state)

@router.message(AnalysisStates.waiting_for_license)
async def process_license_state(message: Message, state: FSMContext):
    codigo = message.text.strip().split()[0]
    await activate_license(message, codigo, state)

@router.message(Command("banca"))
async def cmd_banca(message: Message):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer(get_banca_text(telegram_id), parse_mode="Markdown")
        return
    raw = args[1].strip().replace(".", "").replace(",", "").replace("$", "").replace(" ", "")
    try:
        monto = float(raw)
        if monto <= 0:
            raise ValueError()
        if monto < 10000:
            await message.answer("⚠️ Monto muy bajo, parcero. Ingresa al menos $10.000. Ej: `/banca 200000`")
            return
        set_banca(telegram_id, monto)
        await message.answer(
            f"✅ Banca configurada: {format_pesos(monto)}\n"
            f"📊 Stake prudente → 2% = {format_pesos(monto*0.02)} | 3% = {format_pesos(monto*0.03)}\n"
            f"Listo, mi hermano. Ahora cuando evaluemos una cuota te digo exacto cuánto meterle.",
            reply_markup=kb_proactivo()
        )
    except ValueError:
        await message.answer("❌ Monto inválido. Usa solo números. Ej: `/banca 200000`")

@router.message(Command("generar"))
async def cmd_generar(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ No tienes permisos para usar este comando.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Uso correcto: `/generar <dias>`\nEjemplo: `/generar 30`")
        return
    dias = int(args[1])
    codigo = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM licencias WHERE codigo = ?", (codigo,))
    while cursor.fetchone():
        codigo = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        cursor.execute("SELECT 1 FROM licencias WHERE codigo = ?", (codigo,))
    cursor.execute("INSERT INTO licencias (codigo, dias_duracion, usado) VALUES (?, ?, 0)", (codigo, dias))
    conn.commit()
    conn.close()
    await message.answer(f"🔑 Licencia generada:\n• Código: `{codigo}`\n• Duración: {dias} días\n• Estado: No usada")

# --- HISTORIAL Y CALIFICACIÓN ---
@router.message(Command("historial"))
async def cmd_historial(message: Message):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    text = get_historial_text(telegram_id)
    await message.answer(text, parse_mode="Markdown")

@router.message(Command("calificar"))
async def cmd_calificar(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ Solo el admin puede calificar apuestas.")
        return
    args = message.text.split()
    if len(args) != 3:
        await message.answer("⚠️ Uso: `/calificar <id> <acertado/fallido>`\nEj: `/calificar 12 acertado`")
        return
    try:
        apuesta_id = int(args[1])
    except ValueError:
        await message.answer("❌ ID inválido. Debe ser número. Ej: `/calificar 12 acertado`")
        return
    estado_raw = args[2].lower()
    if estado_raw not in ("acertado", "fallido"):
        await message.answer("❌ Estado inválido. Usa `acertado` o `fallido`.")
        return
    estado = "ACERTADO" if estado_raw == "acertado" else "FALLIDO"
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, mercado, cuota FROM historial_apuestas WHERE id = ?", (apuesta_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        await message.answer(f"❌ No existe apuesta con ID `{apuesta_id}`.")
        return
    cursor.execute("UPDATE historial_apuestas SET estado = ? WHERE id = ?", (estado, apuesta_id))
    conn.commit()
    telegram_id_user = row[0]
    cursor.execute("SELECT COUNT(*), SUM(CASE WHEN estado='ACERTADO' THEN 1 ELSE 0 END), SUM(CASE WHEN estado='FALLIDO' THEN 1 ELSE 0 END) FROM historial_apuestas WHERE telegram_id = ?", (telegram_id_user,))
    total, ac, fa = cursor.fetchone()
    conn.close()
    ac = ac or 0
    fa = fa or 0
    cal = ac + fa
    ef = (ac / cal * 100) if cal > 0 else 0
    emoji = "🟢" if estado == "ACERTADO" else "🔴"
    await message.answer(f"{emoji} Apuesta `#{apuesta_id}` marcada como *{estado}*.\nUsuario `{telegram_id_user}` — Efectividad: {ef:.1f}% ({ac}✅/{cal})", parse_mode="Markdown")

# --- COMBINADAS ---
@router.message(Command("combinada"))
async def cmd_combinada(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    await state.set_state(AnalysisStates.collecting_combinada)
    await state.update_data(partidos_combinada=[])
    await message.answer(
        "🎯 *Modo Combinada activado, mi hermano!*\n\n"
        "Envíame varios enlaces de Flashscore/FotMob uno por uno (Partido 1, Partido 2...).\n"
        "Los voy acumulando sin descontar tu límite diario.\n\n"
        "Cuando termines escribe `/calcular_combinada` o `listo` y te armo el parlay.\n"
        "💡 Tip: puedes mandar 2 a 5 partidos.",
        parse_mode="Markdown",
        reply_markup=kb_combinada()
    )

@router.message(Command("cancelar"))
async def cmd_cancelar_combinada(message: Message, state: FSMContext):
    data = await state.get_data()
    if "partidos_combinada" in data:
        await state.update_data(partidos_combinada=[])
        await state.set_state(AnalysisStates.waiting_for_link)
        await message.answer("❌ Combinada cancelada. Volviste al modo normal. Envíame un enlace suelto si quieres.", reply_markup=kb_proactivo())
    else:
        await state.set_state(AnalysisStates.waiting_for_link)
        await message.answer("✅ Modo normal activado.", reply_markup=kb_proactivo())

@router.message(Command("calcular_combinada"))
async def cmd_calcular_combinada(message: Message, state: FSMContext):
    await procesar_combinada(message, state)

# --- COMANDO HOY / EN VIVO - PARTIDOS EN DIRECTO ---
async def ejecutar_analisis_proactivo(url: str, message: Message, state: FSMContext):
    """Reutiliza el análisis proactivo individual para un URL dado (usado por /hoy y callbacks)."""
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg, reply_markup=kb_proactivo())
        return
    status_msg = await message.answer("🔍 Enlace seleccionado, parcero. Extrayendo vía HTTP ligero (sin navegador, evadiendo Cloudflare)...", reply_markup=kb_proactivo())
    # HTTP directo con headers Chrome real + curl_cffi (TLS fingerprint) + fallback móvil
    try:
        scraped_text, minute_live, score_live = await fetch_match_data(url)
        # Si HTTP no trajo stats, intentar móvil ya está dentro de fetch
        if not scraped_text or len(scraped_text.strip()) < 150:
            await status_msg.edit_text("❌ Flashscore bloqueó la lectura del partido, intenta de nuevo")
            return
        _low = scraped_text.lower()
        _has_stats = any(k in _low for k in ["h2h", "historial", "alineación", "alineacion", "árbitro", "arbitro", "estadística", "estadistica", "head to head", "corners", "córners", "posesión", "posesion", "tarjetas", "goles", "formation", "lineup"])
        if len(scraped_text.strip()) < 150 or not _has_stats:
            # Enviar igual a Gemini si tiene algo, pero advertir; por ahora abortar para no gastar tokens en basura
            await status_msg.edit_text("❌ Flashscore bloqueó la lectura del partido, intenta de nuevo")
            return
        # scraped_text ya trae Estado/Minuto/Marcador formateados correctamente desde fetch_match_data - no reconstruir live_header
    except Exception as e:
        logging.exception(f"Error HTTP ligero: {e}")
        await status_msg.edit_text("❌ Error al extraer datos del partido. Intenta con otro enlace, mi hermano.")
        return
    if not scraped_text.strip():
        await status_msg.edit_text("❌ No se pudo extraer contenido del partido.")
        return
    await state.update_data(scraped_text=scraped_text, last_url=url, minute_live=minute_live, score_live=score_live)
    await status_msg.edit_text(f"📊 Datos listos. Analizando con lupa alineaciones y bajas...")
    try:
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=SYSTEM_INSTRUCTION)
        prompt = f"Datos extraídos (Flashscore/FotMob) - Partido: {url}\n\n{scraped_text}\n\nUsa el Estado/Minuto/Marcador del texto para decidir: si >75' o marcador abultado, prohíbe mercados obsoletos."
        response = await asyncio.to_thread(model.generate_content, prompt)
        analysis_result = response.text.strip() if hasattr(response, "text") and response.text else str(response)
        increment_user_usage(telegram_id)
        await message.answer(analysis_result, reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
        await status_msg.delete()
    except Exception as e:
        logging.error(f'Gemini API Error Detail: {e}')
        logging.exception(f"Gemini traceback: {e}")
        await status_msg.edit_text("❌ Error al conectar con Gemini para el análisis. Intenta de nuevo, parcero.")
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)

@router.message(Command(commands=["hoy", "en_vivo", "envivo"]))
async def cmd_hoy(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    status_msg = await message.answer("🔍 Buscando partidos del día en Flashscore (en vivo + programados), parcero... filtrando los de mayor valor para ti ⏳")
    partidos = []
    try:
        # HTTP ligero para lista del día (sin navegador) - curl_cffi con headers Chrome real
        def _fetch_hoy(u: str) -> str:
            headers = dict(HEADERS_CHROME)
            if HAS_CURL and curl_requests is not None:
                try:
                    resp = curl_requests.get(u, headers=headers, impersonate="chrome110", timeout=15000)
                    if resp.status_code == 200:
                        return resp.text
                except Exception:
                    pass
            try:
                resp = req_requests.get(u, headers=headers, timeout=15000)
                if resp.status_code == 200:
                    return resp.text
            except Exception:
                pass
            return ""
        html = await asyncio.to_thread(_fetch_hoy, "https://www.flashscore.co/")
        if not html or len(html) < 1000:
            html_m = await asyncio.to_thread(_fetch_hoy, "https://m.flashscore.co/")
            if html_m and len(html_m) > len(html):
                html = html_m
        raw = []
        seen = set()
        if HAS_BS4 and html:
            try:
                from bs4 import BeautifulSoup as BS
                soup = BS(html, "html.parser")
                containers = soup.select(".event__match, .event__game, [id^=\"g_1\"] div.event__match, li.event__match")
                anchors_fallback = soup.select('a[href*="/partido/"], a[href*="/match/"]')
                if not containers:
                    containers = []
                def _parse_container(container, href_override=None):
                    href = href_override
                    if not href:
                        a = container.select_one('a[href*="/partido/"], a[href*="/match/"], a')
                        href = a.get("href") if a else None
                        if href and href.startswith("/"):
                            href = "https://www.flashscore.co" + href
                    if not href or href in seen:
                        return None
                    if "/partido/" not in href and "/match/" not in href:
                        return None
                    seen.add(href)
                    txt = container.get_text(separator=" ", strip=True)
                    import re as re_inner
                    minute = None
                    score = None
                    time = None
                    m = re_inner.search(r"\b\d{1,3}(?:\+\d+)?'\b", txt)
                    if m:
                        minute = m.group(0)
                    s = re_inner.search(r"\b\d+\s*[-:]\s*\d+\b", txt)
                    if s:
                        score = s.group(0).replace(" ", "").replace(":", "-")
                    t = re_inner.search(r"\b\d{1,2}:\d{2}\b", txt)
                    if t and not minute:
                        time = t.group(0)
                    parts = container.select(".event__participant")
                    teams = ""
                    if len(parts) >= 2:
                        teams = " vs ".join([pp.get_text(strip=True) for pp in parts[:2]])
                    if not teams or len(teams) < 3:
                        a = container.select_one('a[href*="/partido/"]')
                        if a:
                            teams = a.get_text(strip=True)
                    if not teams or len(teams) < 3:
                        teams = txt.replace(minute or "", "").replace(score or "", "").replace(time or "", "").strip()[:40]
                    teams = " ".join(teams.split())
                    if len(teams) > 32:
                        teams = teams[:32] + "…"
                    if len(teams) < 3:
                        return None
                    is_live = bool(minute or score and "en vivo" in txt.lower())
                    if score and minute:
                        is_live = True
                    league_text = ""
                    parent = container.parent
                    tries = 0
                    while parent and tries < 5:
                        hdr = parent.select_one(".event__header, .sportName")
                        if hdr and hdr.get_text(strip=True):
                            league_text = hdr.get_text(strip=True)
                            break
                        parent = parent.parent
                        tries += 1
                    def get_league_score(t):
                        tu = (t or "").upper()
                        if any(k in tu for k in ["PRIMERA A", "BETPLAY", "DIMAYOR"]):
                            return 100
                        if any(k in tu for k in ["PREMIER LEAGUE", "LA LIGA", "SERIE A", "BUNDESLIGA", "CHAMPIONS", "LIBERTADORES"]):
                            return 95
                        if "AMISTOSO" in tu or "FRIENDLY" in tu:
                            return 10
                        return 35
                    league_score = get_league_score(league_text + " " + txt + " " + teams)
                    if is_live and minute and score:
                        display = f"{minute} [{score}] {teams}"
                    elif is_live and score:
                        display = f"EN VIVO [{score}] {teams}"
                    elif is_live and minute:
                        display = f"{minute} {teams}"
                    elif time:
                        display = f"{time} - {teams}"
                    else:
                        display = teams
                    sort_key = 9999
                    if is_live:
                        try:
                            m2 = int(re_inner.search(r"\d+", minute or "0").group(0))
                        except:
                            m2 = 0
                        sort_key = -1000 + (100 - m2)
                    elif time:
                        try:
                            h, mi = map(int, time.split(":"))
                            sort_key = h*60+mi
                        except:
                            sort_key = 9999
                    attractive = 1 if (is_live and score and score != "0-0") else 0
                    return {"href": href, "text": display, "isLive": is_live, "sortKey": sort_key, "attractive": attractive, "leagueScore": league_score}
                for c in containers:
                    if len(raw) >= 30:
                        break
                    r = _parse_container(c)
                    if r:
                        raw.append(r)
                if len(raw) < 6:
                    for a in anchors_fallback:
                        href = a.get("href")
                        if href and href.startswith("/"):
                            href = "https://www.flashscore.co" + href
                        if not href or href in seen:
                            continue
                        cont = a.find_parent(class_=lambda x: x and "event__match" in x) or a.parent
                        r = _parse_container(cont, href)
                        if r:
                            raw.append(r)
                        if len(raw) >= 30:
                            break
                raw.sort(key=lambda x: (not x["isLive"], -x["leagueScore"], -x["attractive"], x["sortKey"]))
                partidos = [{"href": r["href"], "text": r["text"], "isLive": r["isLive"]} for r in raw[:12]]
                if not partidos:
                    partidos = []
            except Exception as e:
                logging.exception(f"Error HTTP /hoy: {e}")
                partidos = []
    except Exception as e:
        logging.exception(f"Error HTTP /hoy: {e}")
        await status_msg.edit_text("❌ Error al conectar con Flashscore en vivo. Intenta de nuevo en unos segundos, mi hermano.")
        return
    if not partidos:
        await status_msg.edit_text("⚠️ No encontré partidos en vivo ni programados en este momento, parcero. Puede que no haya juegos ahora o Flashscore bloqueó la lectura. Prueba enviando un enlace directo o intenta /hoy en 5 min.", reply_markup=kb_proactivo())
        return
    # Guardar en FSM para callbacks
    await state.update_data(partidos_hoy=partidos)
    await status_msg.delete()
    # Contadores para mensaje inteligente
    cnt_vivo = sum(1 for p in partidos if p.get('isLive'))
    cnt_prog = len(partidos) - cnt_vivo
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for idx, partido in enumerate(partidos):
        # Prefijo visual según estado
        prefix = "🔴" if partido.get('isLive') else "⏰"
        btn_text = partido['text']
        if len(btn_text) > 38:
            btn_text = btn_text[:35] + "…"
        kb.inline_keyboard.append([InlineKeyboardButton(text=f"{prefix} {btn_text}", callback_data=f"hoy_{idx}")])
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔄 Actualizar", callback_data="hoy_refresh"), InlineKeyboardButton(text="❌ Cerrar", callback_data="hoy_close")])
    header = f"🔥 *Top {len(partidos)} más atractivos del día — En vivo: {cnt_vivo} | Programados: {cnt_prog}*"
    await message.answer(
        f"{header}\n"
        f"Prioricé Primera A / Ligas Pro, clásicos y partidos con más expectativa de goles. Formato: `62' [0-3] Equipo vs Equipo` = en vivo | `15:00 - Equipo vs Equipo` = próximo\n"
        f"Toca uno, parcero, y te hago el análisis proactivo con lupa en alineaciones + casa recomendada (BetPlay/Wplay/Codere/Zamba):",
        parse_mode="Markdown",
        reply_markup=kb
    )

# --- VISIÓN ARTIFICIAL: TIQUETE POR FOTO ---
@router.message(F.photo)
async def handle_ticket_photo(message: Message, bot: Bot, state: FSMContext):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    processing = await message.answer("📸 Recibí tu tiquete, parcero — leyendo con visión artificial... ⏳")
    try:
        photo = message.photo[-1]
        file = await bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        buf.seek(0)
        image_bytes = buf.getvalue()
        mime = "image/jpeg"
        if file.file_path.lower().endswith(".png"):
            mime = "image/png"
        elif file.file_path.lower().endswith(".webp"):
            mime = "image/webp"
        elif file.file_path.lower().endswith(".jpg") or file.file_path.lower().endswith(".jpeg"):
            mime = "image/jpeg"

        model = genai.GenerativeModel(model_name="gemini-1.5-flash")
        prompt = (
            "Eres extractor experto de tiquetes de apuestas colombianas (BetPlay, Wplay, Codere, Zamba). "
            "Analiza la captura de pantalla del tiquete y extrae JSON válido con: "
            '{"partido": "Equipo A vs Equipo B", "mercado": "Más de 2.5 goles", "cuota": 1.85}. '
            "Si es combinada/parlay con varios partidos, concatena en partido y toma la cuota total. "
            "Si no ves cuota, pon 0. Responde SOLO JSON, sin texto extra ni markdown."
        )
        response = await asyncio.to_thread(model.generate_content, [prompt, {"mime_type": mime, "data": image_bytes}])
        text = response.text.strip() if hasattr(response, "text") and response.text else ""

        partido = mercado = None
        cuota = 0.0
        try:
            json_match = re.search(r"\{.*\}", text, re.DOTALL)
            if json_match:
                data_json = json.loads(json_match.group(0))
                partido = str(data_json.get("partido", "")).strip() or "Tiquete por foto"
                mercado = str(data_json.get("mercado", "")).strip() or "Mercado por foto"
                cuota = float(str(data_json.get("cuota", 0)).replace(",", "."))
            else:
                raise ValueError("No JSON")
        except Exception:
            partido = "Tiquete por foto"
            mercado = (text[:120].replace("\n", " ").strip() if text else "Mercado por foto")
            m = re.search(r"(\d+[.,]\d+)", text)
            if m:
                try:
                    cuota = float(m.group(1).replace(",", "."))
                except Exception:
                    cuota = 0.0

        if not partido:
            partido = "Tiquete por foto"
        if not mercado:
            mercado = "Mercado por foto"

        fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO historial_apuestas (telegram_id, partido, mercado, cuota, estado, fecha) VALUES (?, ?, ?, ?, 'PENDIENTE', ?)",
            (telegram_id, partido[:300], mercado[:120], cuota, fecha_now)
        )
        conn.commit()
        hid = cursor.lastrowid
        conn.close()

        banca = get_banca(telegram_id)
        if banca and banca > 0:
            stake2 = banca * 0.02
            stake3 = banca * 0.03
            stake_msg = f"\n💰 Banca {format_pesos(banca)} → 2% {format_pesos(stake2)} | 3% {format_pesos(stake3)}\n👉 Sugerido: {format_pesos(stake3)} (3% valor)"
        else:
            stake_msg = "\n💡 Configura tu banca con /banca 200000 para cálculo de stake"

        await processing.edit_text(
            f"📸 *¡Tiquete leído, mi hermano!* ✅\n\n"
            f"🏟️ Partido: {partido}\n"
            f"🎯 Mercado: {mercado}\n"
            f"💵 Cuota: {cuota:.2f}\n\n"
            f"📝 Guardado en historial como `#{hid}` ⏳ Pendiente — ver con /historial"
            f"{stake_msg}",
            parse_mode="Markdown",
            reply_markup=kb_cuota()
        )
        await state.update_data(scraped_text=f"Tiquete foto: {partido} - {mercado}", last_url=partido)
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
    except Exception as e:
        logging.exception(f"Error visión tiquete: {e}")
        await processing.edit_text("❌ No pude leer tu tiquete, parcero. Asegúrate que la foto sea nítida (que se vean BetPlay/Wplay/Codere/Zamba, partido, mercado y cuota) y reenvíala sin recortar la cuota total.")

async def procesar_combinada(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    data = await state.get_data()
    partidos = data.get("partidos_combinada", [])
    if not partidos:
        await message.answer("⚠️ No has agregado partidos, parcero. Usa /combinada y envía al menos 2 enlaces.", reply_markup=kb_combinada())
        return
    if len(partidos) < 2:
        await message.answer(f"⚠️ Llevas solo {len(partidos)} partido. Para combinada necesitas mínimo 2. Envía otro enlace.", reply_markup=kb_combinada())
        return
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    status_msg = await message.answer(f"🔍 Procesando combinada de {len(partidos)} partidos... vía HTTP ligero (FotMob/Flashscore)")
    textos_combinados = []
    for idx, url in enumerate(partidos, 1):
        try:
            txt, minute, score = await fetch_match_data(url)
            if not txt or len(txt.strip()) < 80:
                txt = f"[Datos limitados para {url}]"
            header = f"MINUTO: {minute or 'No iniciado'} | MARCADOR: {score or '0-0'}"
            textos_combinados.append(f"--- PARTIDO {idx}: {url} ---\n{header}\n{txt[:2500]}")
            await asyncio.sleep(random.uniform(0.4, 0.9))
        except Exception as e:
            logging.exception(f"Error HTTP combinada {idx}: {e}")
            textos_combinados.append(f"--- PARTIDO {idx}: {url} ---\n[Error HTTP]")
    if not textos_combinados:
        await status_msg.edit_text("❌ No se pudo extraer datos.")
        return
    await status_msg.edit_text("📊 Datos leídos. Calculando viabilidad conjunta del parlay...")
    try:
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=SYSTEM_INSTRUCTION_COMBINADA)
        prompt = f"Datos combinados para Combinada/Parlay (Flashscore/FotMob) de {len(partidos)} partidos:\n\n" + "\n\n".join(textos_combinados)
        prompt += "\n\nRecuerda: revisa alineaciones y bajas, no inventes cuotas, 1 línea por selección, indica casa y viabilidad conjunta."
        response = await asyncio.to_thread(model.generate_content, prompt)
        result = response.text.strip() if hasattr(response, "text") and response.text else str(response)
        increment_user_usage(telegram_id)
        combinado_text = "\n\n".join(textos_combinados)
        await state.update_data(scraped_text=combinado_text, last_url=f"Combinada {len(partidos)} partidos")
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
        await message.answer(result + f"\n\n✅ Combinada procesada. Descontado 1 uso de tus 10 diarios.", reply_markup=kb_cuota())
        await status_msg.delete()
    except Exception as e:
        logging.error(f'Gemini API Error Detail: {e}')
        logging.exception(f"Gemini traceback: {e}")
        await status_msg.edit_text("❌ Error al calcular la combinada con Gemini. Intenta de nuevo, parcero.")

# --- CALLBACKS INLINE KEYBOARDS ---
@router.callback_query(F.data == "ver_historial")
async def cb_ver_historial(callback: CallbackQuery):
    await callback.answer()
    telegram_id = callback.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await callback.message.answer(err_msg)
        return
    text = get_historial_text(telegram_id)
    await callback.message.answer(text, parse_mode="Markdown")

@router.callback_query(F.data == "consultar_banca")
async def cb_consultar_banca(callback: CallbackQuery):
    await callback.answer()
    telegram_id = callback.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await callback.message.answer(err_msg)
        return
    await callback.message.answer(get_banca_text(telegram_id), parse_mode="Markdown")

@router.callback_query(F.data == "modo_combinada")
async def cb_modo_combinada(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Reusar lógica de /combinada
    telegram_id = callback.from_user.id
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await callback.message.answer(err_msg)
        return
    await state.set_state(AnalysisStates.collecting_combinada)
    await state.update_data(partidos_combinada=[])
    await callback.message.answer(
        "🎯 *Modo Combinada activado!*\nEnvíame enlaces de Flashscore/FotMob uno por uno.\nCuando termines pulsa 🔥 Calcular Parlay.",
        parse_mode="Markdown",
        reply_markup=kb_combinada()
    )

@router.callback_query(F.data == "agregar_otro")
async def cb_agregar_otro(callback: CallbackQuery):
    await callback.answer("Envíame el siguiente enlace de Flashscore, parcero 📎", show_alert=False)
    await callback.message.answer("📎 Listo, parcero — envíame el siguiente enlace de Flashscore para la combinada.\nLlevas tiempo, sin afán. Cuando termines pulsa 🔥 Calcular Parlay.", reply_markup=kb_combinada())

@router.callback_query(F.data == "calcular_parlay")
async def cb_calcular_parlay(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Calculando parlay, mi hermano... ⏳")
    # Crear un mensaje ficticio para procesar_combinada (usa callback.message como contexto)
    await procesar_combinada(callback.message, state)

@router.callback_query(F.data == "cancelar_combinada")
async def cb_cancelar_combinada(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Combinada cancelada")
    await state.update_data(partidos_combinada=[])
    await state.set_state(AnalysisStates.waiting_for_link)
    await callback.message.answer("❌ Combinada cancelada. Modo normal activado, parcero.", reply_markup=kb_proactivo())
    await callback.message.edit_reply_markup(reply_markup=None)

# --- CALLBACKS HOY / EN VIVO ---
@router.callback_query(F.data.startswith("hoy_"))
async def cb_hoy_selector(callback: CallbackQuery, state: FSMContext):
    data = callback.data
    if data == "hoy_refresh":
        await callback.answer("Actualizando... ⏳")
        try:
            await callback.message.delete()
        except Exception:
            pass
        # Re-ejecutar /hoy correctamente con el usuario que clickeó
        # Usamos el mismo callback.message pero forzamos el check con callback.from_user
        class _FakeMsg:
            def __init__(self, msg, user):
                self._msg = msg
                self.from_user = user
                self.chat = msg.chat
            def __getattr__(self, name):
                return getattr(self._msg, name)
            async def answer(self, *a, **kw):
                return await self._msg.answer(*a, **kw)
        fake = _FakeMsg(callback.message, callback.from_user)
        await cmd_hoy(fake, state)
        return
    if data == "hoy_close":
        await callback.answer()
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer("✅ Cerrado, parcero. Usa /hoy cuando quieras ver los en vivo.", reply_markup=kb_proactivo())
        return
    # Caso hoy_0, hoy_1 ... -> análisis proactivo
    if data.startswith("hoy_") and data[4:].isdigit():
        try:
            idx = int(data.split("_")[1])
        except Exception:
            await callback.answer("Error al leer partido")
            return
        fsm_data = await state.get_data()
        partidos = fsm_data.get("partidos_hoy", [])
        if idx < 0 or idx >= len(partidos):
            await callback.answer("Partido ya no disponible, actualiza con /hoy", show_alert=True)
            return
        url = partidos[idx].get("href")
        nombre = partidos[idx].get("text", f"Partido {idx+1}")
        await callback.answer(f"Analizando {nombre[:25]}... ⏳")
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        await callback.message.answer(f"⚽ Elegiste: *{nombre}*\n🔗 {url}\n\nIniciando análisis proactivo...", parse_mode="Markdown")
        await ejecutar_analisis_proactivo(url, callback.message, state)
        return
    await callback.answer()

# --- Flujo principal proactivo ---
@router.message(F.text & ~F.text.startswith("/"))
async def handle_user_flow(message: Message, state: FSMContext):
    current_state = await state.get_state()
    telegram_id = message.from_user.id
    text = message.text.strip()
    text_lower = text.lower()

    if current_state == AnalysisStates.collecting_combinada.state:
        if text_lower in ("listo", "listo!", "calcular", "calcular_combinada"):
            await procesar_combinada(message, state)
            return
        if "flashscore" in text_lower or "fotmob" in text_lower:
            data = await state.get_data()
            partidos = data.get("partidos_combinada", [])
            if text in partidos:
                await message.answer(f"⚠️ Ese partido ya está agregado. Llevas {len(partidos)}: envía otro diferente.", reply_markup=kb_combinada())
                return
            partidos.append(text)
            await state.update_data(partidos_combinada=partidos)
            await message.answer(
                f"✅ Partido {len(partidos)} agregado.\n"
                f"📋 Llevas {len(partidos)} en la combinada.",
                reply_markup=kb_combinada()
            )
            return
        await message.answer(
            f"🎯 Modo Combinada: llevas {len((await state.get_data()).get('partidos_combinada', []))} partidos.\n"
            f"Envíame enlaces de Flashscore/FotMob.",
            reply_markup=kb_combinada()
        )
        return

    # === PRIORIDAD 1: Análisis Individual (fuera de combinada) ===
    # Si es enlace Flashscore y NO está en modo combinada, ejecutar análisis individual de inmediato
    if "flashscore" in text_lower or "fotmob" in text_lower and current_state != AnalysisStates.collecting_combinada.state:
        # No confundirse con estados previos (waiting_for_quota_or_chat), va directo a flujo individual abajo
        pass
    elif current_state in (AnalysisStates.waiting_for_quota_or_chat.state, AnalysisStates.waiting_for_quota.state):
        # === CAPTURA DE SELECCIÓN POST-PROACTIVO ===
        # El bot acaba de enviar "¿Cuál te gusta o qué cuota te ofrece tu casa...?" y queda a la espera.
        # Cualquier respuesta con mercado/cuota (ej. "me fui con los córners del Everton a 1.65") se procesa aquí:
        # -> calcula EV, stake 2%/3% según banca_actual y guarda en historial_apuestas como PENDIENTE
        if text_lower.startswith("combinada"):
            await cmd_combinada(message, state)
            return
        await process_quota_chat(message, state)
        return

    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    url = text
    if "flashscore" not in url.lower() and "fotmob" not in url.lower():
        if url.startswith("http"):
            await message.answer("⚠️ Por favor envía un enlace válido de Flashscore o FotMob (debe contener 'flashscore' o 'fotmob').\nSi quieres evaluar una cuota escríbela así: `1.90 al ambos anotan`\n🎯 Para parlays usa /combinada", reply_markup=kb_proactivo())
        return
    status_msg = await message.answer("🔍 Enlace válido, parcero. Extrayendo vía HTTP ligero (sin navegador)...", reply_markup=kb_proactivo())
    try:
        scraped_text, minute_live, score_live = await fetch_match_data(url)
        if not scraped_text or len(scraped_text.strip()) < 150:
            await status_msg.edit_text("❌ Flashscore bloqueó la lectura del partido, intenta de nuevo")
            return
        _low = scraped_text.lower()
        _has_stats = any(k in _low for k in ["h2h", "historial", "alineación", "alineacion", "árbitro", "arbitro", "estadística", "estadistica", "head to head", "corners", "córners", "posesión", "posesion", "tarjetas", "goles", "formation", "lineup"])
        if len(scraped_text.strip()) < 150 or not _has_stats:
            await status_msg.edit_text("❌ Flashscore bloqueó la lectura del partido, intenta de nuevo")
            return
        # scraped_text ya trae Estado/Minuto/Marcador formateados correctamente desde fetch_match_data - no reconstruir live_header
    except Exception as e:
        logging.exception(f"Error HTTP ligero: {e}")
        await status_msg.edit_text("❌ Error al extraer datos del partido. Intenta de nuevo, parcero.")
        return
    if not scraped_text.strip():
        await status_msg.edit_text("❌ No se pudo extraer contenido. El sitio puede estar bloqueando.")
        return
    await state.update_data(scraped_text=scraped_text, last_url=url, minute_live=minute_live, score_live=score_live)
    await status_msg.edit_text(f"📊 Datos listos. Analizando con lupa alineaciones y bajas...")
    try:
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=SYSTEM_INSTRUCTION)
        prompt = f"Datos extraídos (Flashscore/FotMob) - Partido: {url}\n\n{scraped_text}\n\nUsa el Estado/Minuto/Marcador del texto para decidir: si >75' o marcador abultado, prohíbe mercados obsoletos."
        response = await asyncio.to_thread(model.generate_content, prompt)
        analysis_result = response.text.strip() if hasattr(response, "text") and response.text else str(response)
        increment_user_usage(telegram_id)
        await message.answer(analysis_result, reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
    except Exception as e:
        logging.error(f'Gemini API Error Detail: {e}')
        logging.exception(f"Gemini traceback: {e}")
        await message.answer("❌ Error al conectar con Gemini para el análisis proactivo. Intenta de nuevo en unos segundos, parcero.", reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)

async def process_quota_chat(message: Message, state: FSMContext):
    """Captura la elección del usuario post-proactivo, calcula EV + stake y guarda en historial como PENDIENTE."""
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    # Captura de la elección: puede ser "me fui con los córners del Everton a 1.65" o "1.90 ambos anotan"
    quota_input = message.text.strip()
    data = await state.get_data()
    scraped_text = data.get("scraped_text", "")
    last_url = data.get("last_url", "partido anterior")
    if not scraped_text:
        await message.answer("⚠️ No tengo contexto del partido, mi hermano. Envíame primero un enlace de Flashscore/FotMob y luego evaluamos la cuota que quieras.", reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_link)
        return
    if len(quota_input) < 3:
        await message.answer("Pilas pues, dime la cuota y mercado: ej. `1.90 al ambos anotan` o `2.10 en +4.5 tarjetas` y te digo si hay valor o si nos quemamos.", reply_markup=kb_proactivo())
        return
    processing_msg = await message.answer("🤖 Analizando tu elección, parcero... calculando EV y stake...")
    try:
        model = genai.GenerativeModel(model_name="gemini-1.5-flash", system_instruction=SYSTEM_INSTRUCTION_CUOTA)
        prompt = (
            f"Contexto del partido (Flashscore/FotMob - {last_url}):\n{scraped_text}\n\n"
            f"Consulta del usuario - Cuota y mercado a evaluar (BetPlay/Wplay/Codere/Zamba):\n{quota_input}\n\n"
            f"Recuerda: revisa alineaciones y bajas con lupa, calcula 1/Cuota, EV >5% = VALOR. Menciona casa recomendada (BetPlay, Wplay, Codere o Zamba)."
        )
        response = await asyncio.to_thread(model.generate_content, prompt)
        result = response.text.strip() if hasattr(response, "text") and response.text else str(response)
        try:
            match = re.search(r"(\d+[.,]\d+)", quota_input)
            if match:
                cuota_val = float(match.group(1).replace(",", "."))
            else:
                m2 = re.search(r"(\d+)", quota_input)
                cuota_val = float(m2.group(1)) if m2 else 0.0
            mercado_txt = quota_input[:120].strip()
            partido_txt = last_url[:300] if last_url else scraped_text[:80]
            fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO historial_apuestas (telegram_id, partido, mercado, cuota, estado, fecha)
                VALUES (?, ?, ?, ?, 'PENDIENTE', ?)
            """, (telegram_id, partido_txt, mercado_txt, cuota_val, fecha_now))
            conn.commit()
            historial_id = cursor.lastrowid
            conn.close()
        except Exception as e:
            logging.exception(f"Error guardando historial: {e}")
            historial_id = None
        banca = get_banca(telegram_id)
        stake_msg = ""
        es_valor = "🟢" in result or "METALE" in result.upper() or "CONFIANZA" in result.upper()
        if banca and banca > 0:
            stake2 = banca * 0.02
            stake3 = banca * 0.03
            sugerido = stake3 if es_valor else stake2
            stake_msg = (
                f"\n\n💰 Banca {format_pesos(banca)} → Stake prudente: 2% {format_pesos(stake2)} | 3% {format_pesos(stake3)}\n"
                f"👉 Sugerido para esta: {format_pesos(sugerido)} ({'3% valor' if es_valor else '2% conservador'})"
            )
        else:
            stake_msg = "\n\n💡 ¿Quieres que te calcule cuánto apostar? Configura tu banca: `/banca 200000`"
        historial_msg = f"\n\n📝 Guardado en historial como `#{historial_id}` ⏳ Pendiente" if historial_id else ""
        await processing_msg.edit_text(result + stake_msg + historial_msg, parse_mode="Markdown")
        # Teclado post-evaluación con historial y casas
        await message.answer("¿Qué hacemos ahora, parcero?", reply_markup=kb_cuota())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
    except Exception as e:
        logging.error(f'Gemini API Error Detail: {e}')
        logging.exception(f"Gemini traceback: {e}")
        await processing_msg.edit_text("❌ Error al evaluar la cuota con Gemini. Intenta de nuevo, mi hermano.")

# ==========================================
# 6. MAIN - Arranque del bot
# ==========================================
async def main():
    init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logging.info("Bot iniciado correctamente (aiogram v3.x) - Inline Keyboards + Historial")
    await dp.start_polling(bot)

# ==========================================
# 7. SERVIDOR WEB FALSO PARA RENDER (evita Timed Out)
# ==========================================
import threading
from flask import Flask

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return '¡El Bot de Apuestas está activo 24/7, mi hermano! 🚀⚽'

def run_web():
    port = int(os.getenv('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

if __name__ == '__main__':
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info('Bot detenido.')