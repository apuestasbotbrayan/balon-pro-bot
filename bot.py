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

from openai import OpenAI
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
    def _fetch(u: str) -> str:
        headers = dict(HEADERS_CHROME)
        if HAS_CURL and curl_requests is not None:
            try:
                resp = curl_requests.get(u, headers=headers, impersonate="chrome110", timeout=15000)
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text
            except Exception:
                pass
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
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()
                detail = soup.select_one("#detail, .container__detail")
                if detail:
                    text = detail.get_text(separator="\n", strip=True)
                else:
                    text = soup.get_text(separator="\n", strip=True)
            except Exception:
                text = re.sub(r"<[^>]+>", "\n", html)
        else:
            text = re.sub(r"<[^>]+>", "\n", html)
        text = re.sub(r"\n{2,}", "\n", text).strip()[:4000]
        m = re.search(r"\b\d{1,3}(?:\+\d+)?'\b", text)
        if m:
            minute = m.group(0)
        s = re.search(r"\b\d+\s*[-:]\s*\d+\b", text)
        if s:
            score = s.group(0).replace(" ", "").replace(":", "-")
        return text, minute, score

    candidates = [url, _to_mobile_url(url)]
    for cand in candidates:
        html = await asyncio.to_thread(_fetch, cand)
        txt, minute, score = _extract(html)
        _low = txt.lower()
        has_stats = any(k in _low for k in ["h2h", "historial", "alineación", "alineacion", "árbitro", "arbitro", "estadística", "formation", "lineup", "corners", "goles", "tarjetas", "posesión"])
        if len(txt.strip()) > 400 or has_stats:
            return txt[:3000], minute, score
        if len(txt.strip()) > 150:
            last = (txt[:3000], minute, score)
        else:
            last = ("", "", "")
    try:
        return last
    except Exception:
        return "", "", ""

async def fetch_fotmob_data(url: str) -> tuple[str, str, str]:
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
    low = url.lower()
    if "fotmob.com" in low:
        txt, minute, score = await fetch_fotmob_data(url)
        if txt and len(txt.strip()) > 80:
            return txt, minute, score
    return await fetch_flashscore_text(url)

# ==========================================
# 1. CONFIGURACIÓN Y CLIENTE XAI (GROK)
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8785828541:AAHuZoLPpmwDYXzXl92b_PxMDxJ3jpY0Q6g")
XAI_API_KEY = os.getenv("XAI_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8021280020"))

DB_NAME = "bot_database.db"

# Inicializar cliente OpenAI apuntando a xAI endpoint
xai_client = OpenAI(
    api_key=XAI_API_KEY,
    base_url="https://api.x.ai/v1"
)
GROK_MODEL = "grok-beta"

async def grok_generate_with_retry(system_instruction: str, user_prompt: str, max_retries: int = 1):
    """Llamada a Grok blindada con reintentos automáticos."""
    for attempt in range(max_retries + 1):
        try:
            response = await asyncio.to_thread(
                xai_client.chat.completions.create,
                model=GROK_MODEL,
                messages=[
                    {"role": "system", "content": system_instruction},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.7
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            msg = str(e).lower()
            if attempt < max_retries:
                logging.warning(f"Grok reintentando por error: {e}")
                await asyncio.sleep(2)
                continue
            raise

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
    "Cierra SIEMPRE exactamente con: '¿Cuál te gusta o qué cuota te ofrece tu casa de apuestas para calcular si le apostamos?'"
)

SYSTEM_INSTRUCTION_CUOTA = (
    "Actúa como tipster colombiano parcero firme y directo. Cuando el usuario te dé una cuota/mercado, "
    "calcula Probabilidad Implícita (1/Cuota) y EV internamente pero NO muestres fórmula larga. "
    "Responde AL GRANO en MÁXIMO 2 LÍNEAS: "
    "Línea 1: veredicto en una sola frase con emoji: si EV >5% -> '🟢 ¡Métale con confianza! (EV +X%)' "
    "si no -> '🔴 ¡Pilas, no bote la plata por ahí! (EV -X% / Sin valor)'. "
    "Línea 2: justificación técnica ultra corta en 1 frase + menciona casa recomendada: '📍 Búscala en [BetPlay/Wplay/Codere/Zamba]'."
)

SYSTEM_INSTRUCTION_COMBINADA = (
    "Actúa como tipster profesional colombiano parcero experto en combinadas/parlays. "
    "Recibes datos de VARIOS partidos de Flashscore/FotMob. Sé ULTRA RESUMIDO y VISUAL, sin floro. "
    "Propón 1 combinada principal de 2-3 selecciones en formato: "
    "'🔥 [Partido - Mercado]: [por qué 10 palabras] | 📍 BetPlay/Wplay/Codere/Zamba'. "
    "Cierra con: '¿Cuál te gusta o qué cuota te ofrece tu casa de apuestas para calcular si le apostamos?'"
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
# 3. CAPA DE DATOS (SQLite)
# ==========================================
def init_db():
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
    try:
        exp_date = datetime.strptime(fecha_expiracion_str, "%Y-%m-%d")
        if datetime.now().date() > exp_date.date():
            return False, "❌ Tu licencia ha expirado. Contacta al administrador para renovarla."
    except Exception:
        return False, "❌ Error al verificar la expiración de tu licencia."
    today_str = datetime.now().strftime("%Y-%m-%d")
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
        f"📎 Pilas pues, envíame un enlace de Flashscore y te saco de una las mejores opciones de valor."
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
                await message.answer("✅ Licencia activa, parcero. Envíame tu enlace de Flashscore y lo analizamos de una.", reply_markup=kb_proactivo())
                return
        except Exception:
            pass
    if len(args) > 1:
        await activate_license(message, args[1].strip(), state)
        return
    await message.answer(
        "👋 ¡Bienvenido al Tipster Bot Pro, mi hermano!\n\n"
        "🔒 Para acceder, envía tu código de licencia o usa `/activar TU_CODIGO`"
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
        set_banca(telegram_id, monto)
        await message.answer(
            f"✅ Banca configurada: {format_pesos(monto)}\n"
            f"📊 Stake prudente → 2% = {format_pesos(monto*0.02)} | 3% = {format_pesos(monto*0.03)}",
            reply_markup=kb_proactivo()
        )
    except ValueError:
        await message.answer("❌ Monto inválido. Ej: `/banca 105537`")

@router.message(Command("generar"))
async def cmd_generar(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("⛔ No tienes permisos.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2 or not args[1].isdigit():
        await message.answer("⚠️ Uso: `/generar <dias>`")
        return
    dias = int(args[1])
    codigo = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO licencias (codigo, dias_duracion, usado) VALUES (?, ?, 0)", (codigo, dias))
    conn.commit()
    conn.close()
    await message.answer(f"🔑 Licencia generada:\n• Código: `{codigo}`\n• Duración: {dias} días")

@router.message(Command("historial"))
async def cmd_historial(message: Message):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_valid(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    await message.answer(get_historial_text(telegram_id), parse_mode="Markdown")

@router.message(Command("combinada"))
async def cmd_combinada(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    await state.set_state(AnalysisStates.collecting_combinada)
    await state.update_data(partidos_combinada=[])
    await message.answer("🎯 *Modo Combinada activado!*\nEnvíame enlaces uno por uno.", parse_mode="Markdown", reply_markup=kb_combinada())

@router.message(Command("cancelar"))
async def cmd_cancelar_combinada(message: Message, state: FSMContext):
    await state.set_state(AnalysisStates.waiting_for_link)
    await message.answer("✅ Modo normal activado.", reply_markup=kb_proactivo())

@router.message(Command("calcular_combinada"))
async def cmd_calcular_combinada(message: Message, state: FSMContext):
    await procesar_combinada(message, state)

async def ejecutar_analisis_proactivo(url: str, message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg, reply_markup=kb_proactivo())
        return
    status_msg = await message.answer("🔍 Enlace seleccionado. Extrayendo datos...")
    try:
        scraped_text, minute_live, score_live = await fetch_match_data(url)
        if not scraped_text or len(scraped_text.strip()) < 150:
            await status_msg.edit_text("❌ No se pudo leer el partido, intenta de nuevo.")
            return
    except Exception:
        await status_msg.edit_text("❌ Error al extraer datos del partido.")
        return
    await state.update_data(scraped_text=scraped_text, last_url=url)
    await status_msg.edit_text("📊 Analizando con Grok (xAI)...")
    try:
        prompt = f"Datos extraídos - Partido: {url}\n\n{scraped_text}"
        analysis_result = await grok_generate_with_retry(SYSTEM_INSTRUCTION, prompt)
        increment_user_usage(telegram_id)
        await message.answer(analysis_result, reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
        await status_msg.delete()
    except Exception as e:
        logging.error(f'Grok API Error: {e}')
        await status_msg.edit_text("❌ Error al conectar con Grok. Intenta de nuevo, parcero.")

async def procesar_combinada(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    data = await state.get_data()
    partidos = data.get("partidos_combinada", [])
    if len(partidos) < 2:
        await message.answer("⚠️ Necesitas mínimo 2 partidos para combinada.", reply_markup=kb_combinada())
        return
    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    status_msg = await message.answer(f"🔍 Procesando combinada de {len(partidos)} partidos...")
    textos = []
    for idx, url in enumerate(partidos, 1):
        try:
            txt, _, _ = await fetch_match_data(url)
            textos.append(f"--- PARTIDO {idx}: {url} ---\n{txt[:2000]}")
        except Exception:
            textos.append(f"--- PARTIDO {idx}: {url} ---\n[Error]")
    try:
        prompt = f"Datos combinados para Parlay de {len(partidos)} partidos:\n\n" + "\n\n".join(textos)
        result = await grok_generate_with_retry(SYSTEM_INSTRUCTION_COMBINADA, prompt)
        increment_user_usage(telegram_id)
        await state.update_data(scraped_text="\n".join(textos), last_url=f"Combinada {len(partidos)} partidos")
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
        await message.answer(result + "\n\n✅ Combinada procesada.", reply_markup=kb_cuota())
        await status_msg.delete()
    except Exception as e:
        logging.error(f'Grok API Error: {e}')
        await status_msg.edit_text("❌ Error al calcular la combinada.")

@router.callback_query(F.data == "ver_historial")
async def cb_ver_historial(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_historial_text(callback.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "consultar_banca")
async def cb_consultar_banca(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_banca_text(callback.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "modo_combinada")
async def cb_modo_combinada(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(AnalysisStates.collecting_combinada)
    await state.update_data(partidos_combinada=[])
    await callback.message.answer("🎯 *Modo Combinada activado!*\nEnvíame enlaces uno por uno.", parse_mode="Markdown", reply_markup=kb_combinada())

@router.callback_query(F.data == "agregar_otro")
async def cb_agregar_otro(callback: CallbackQuery):
    await callback.answer("Envía el siguiente enlace", show_alert=False)
    await callback.message.answer("📎 Envía el siguiente enlace de partido.", reply_markup=kb_combinada())

@router.callback_query(F.data == "calcular_parlay")
async def cb_calcular_parlay(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Calculando parlay... ⏳")
    await procesar_combinada(callback.message, state)

@router.callback_query(F.data == "cancelar_combinada")
async def cb_cancelar_combinada(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Cancelado")
    await state.set_state(AnalysisStates.waiting_for_link)
    await callback.message.answer("❌ Combinada cancelada.", reply_markup=kb_proactivo())

@router.message(F.text & ~F.text.startswith("/"))
async def handle_user_flow(message: Message, state: FSMContext):
    current_state = await state.get_state()
    telegram_id = message.from_user.id
    text = message.text.strip()
    text_lower = text.lower()

    if current_state == AnalysisStates.collecting_combinada.state:
        if text_lower in ("listo", "calcular", "calcular_combinada"):
            await procesar_combinada(message, state)
            return
        if "flashscore" in text_lower or "fotmob" in text_lower:
            data = await state.get_data()
            partidos = data.get("partidos_combinada", [])
            partidos.append(text)
            await state.update_data(partidos_combinada=partidos)
            await message.answer(f"✅ Partido agregado ({len(partidos)}).", reply_markup=kb_combinada())
            return
        return

    if current_state in (AnalysisStates.waiting_for_quota_or_chat.state, AnalysisStates.waiting_for_quota.state):
        if text_lower.startswith("combinada"):
            await cmd_combinada(message, state)
            return
        await process_quota_chat(message, state)
        return

    allowed, err_msg = check_user_access(telegram_id)
    if not allowed:
        await message.answer(err_msg)
        return
    if "flashscore" not in text.lower() and "fotmob" not in text.lower():
        await message.answer("⚠️ Envía un enlace válido de Flashscore o FotMob.", reply_markup=kb_proactivo())
        return
    
    status_msg = await message.answer("🔍 Enlace válido. Extrayendo...", reply_markup=kb_proactivo())
    try:
        scraped_text, minute_live, score_live = await fetch_match_data(text)
        if not scraped_text or len(scraped_text.strip()) < 150:
            await status_msg.edit_text("❌ No se pudo leer el partido, intenta de nuevo.")
            return
    except Exception:
        await status_msg.edit_text("❌ Error al extraer datos.")
        return

    await state.update_data(scraped_text=scraped_text, last_url=text)
    await status_msg.edit_text("📊 Analizando con Grok...")
    try:
        prompt = f"Datos del partido:\n\n{scraped_text}"
        analysis_result = await grok_generate_with_retry(SYSTEM_INSTRUCTION, prompt)
        increment_user_usage(telegram_id)
        await message.answer(analysis_result, reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
    except Exception as e:
        logging.error(f'Grok API Error: {e}')
        await message.answer("❌ Error al conectar con Grok.", reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)

async def process_quota_chat(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    quota_input = message.text.strip()
    data = await state.get_data()
    scraped_text = data.get("scraped_text", "")
    last_url = data.get("last_url", "partido")
    if not scraped_text:
        await message.answer("⚠️ Envía primero un enlace de partido.", reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_link)
        return
    processing_msg = await message.answer("🤖 Evaluando tu cuota...")
    try:
        prompt = f"Contexto:\n{scraped_text}\n\nCuota elegida:\n{quota_input}"
        result = await grok_generate_with_retry(SYSTEM_INSTRUCTION_CUOTA, prompt)
        
        cuota_val = 0.0
        try:
            match = re.search(r"(\d+[.,]\d+)", quota_input)
            if match:
                cuota_val = float(match.group(1).replace(",", "."))
        except Exception:
            pass

        fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M")
        conn = sqlite3.connect(DB_NAME)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO historial_apuestas (telegram_id, partido, mercado, cuota, estado, fecha) VALUES (?, ?, ?, ?, 'PENDIENTE', ?)", 
                       (telegram_id, last_url[:300], quota_input[:120], cuota_val, fecha_now))
        conn.commit()
        hid = cursor.lastrowid
        conn.close()

        banca = get_banca(telegram_id)
        stake_msg = f"\n\n📝 Guardado en historial `#{hid}` ⏳"
        if banca > 0:
            stake_msg += f"\n💰 Sugerido Stake (3%): {format_pesos(banca*0.03)}"

        await processing_msg.edit_text(result + stake_msg, parse_mode="Markdown")
        await message.answer("¿Qué hacemos ahora, parcero?", reply_markup=kb_cuota())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
    except Exception as e:
        logging.error(f'Grok API Error: {e}')
        await processing_msg.edit_text("❌ Error al evaluar la cuota.")

# ==========================================
# 6. MAIN & FLASK
# ==========================================
import threading
from flask import Flask

web_app = Flask(__name__)

@web_app.route('/')
def home():
    return '¡El Bot de Apuestas con Grok está activo 24/7, mi hermano! 🚀⚽'

def run_web():
    port = int(os.getenv('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)

async def main():
    init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    bot = Bot(token=TELEGRAM_TOKEN)
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    logging.info("Bot iniciado correctamente con Grok (aiogram v3.x)")
    await dp.start_polling(bot)

if __name__ == '__main__':
    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info('Bot detenido.')