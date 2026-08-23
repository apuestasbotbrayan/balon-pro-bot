import asyncio
import io
import json
import logging
import os
import random
import re
import sqlite3
import string
import threading
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Update
from flask import Flask, request

# Cliente oficial moderno de Google GenAI
from google import genai
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

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
                resp = curl_requests.get(u, headers=headers, impersonate="chrome110", timeout=15)
                if resp.status_code == 200 and len(resp.text) > 500:
                    return resp.text
            except Exception:
                pass
        try:
            resp = req_requests.get(u, headers=headers, timeout=15)
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
                resp = curl_requests.get(api_url, headers=headers, impersonate="chrome110", timeout=15)
                if resp.status_code == 200:
                    return resp.json()
            except Exception:
                pass
        try:
            resp = req_requests.get(api_url, headers=headers, timeout=15)
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
# 1. CONFIGURACIÓN CLIENTE OFICIAL GOOGLE GENAI
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8785828541:AAHuZoLPpmwDYXzXl92b_PxMDxJ3jpY0Q6g")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "8021280020"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://balon-pro-bot.onrender.com")

DB_NAME = "bot_database.db"

ai_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL_ID = "gemini-2.5-flash"

async def gemini_generate_with_retry(system_instruction: str, user_prompt: str, max_retries: int = 2):
    full_prompt = f"{system_instruction}\n\n{user_prompt}"
    for attempt in range(max_retries + 1):
        try:
            response = await asyncio.to_thread(
                ai_client.models.generate_content,
                model=GEMINI_MODEL_ID,
                contents=full_prompt,
            )
            return response.text.strip()
        except Exception as e:
            logging.warning(f"Gemini intento {attempt + 1} fallido por error: {e}")
            if attempt < max_retries:
                await asyncio.sleep(2)
                continue
            raise

SYSTEM_INSTRUCTION = (
    "Actúa como tipster profesional colombiano parcero experto. Cuando recibas datos de un partido (Flashscore/FotMob), "
    "REVISA CON LUPA las alineaciones probables y bajas de jugadores clave, además de árbitro, H2H y tendencias. "
    "SÉ ULTRA RESUMIDO y VISUAL, sin floro. "
    "Saluda breve parcero ('¡Epa, mi hermano!') y presenta de una las 2 o 3 mejores Value Bets SIN INVENTAR CUOTAS FIJAS. "
    "PROHIBIDO poner números de cuota falsos (no escribas 'Cuota 1.65' si no te la dieron). "
    "Formato obligatorio, máximo 1 línea por opción: '🔥 [Mercado]: [por qué en máx 15 palabras] | 📍 Busca en [BetPlay/Wplay/Codere/Zamba]'. "
    "Cierra SIEMPRE exactamente con: '¿Cuál te gusta o qué cuota te ofrece tu casa de apuestas para calcular si le apostamos?'"
)

SYSTEM_INSTRUCTION_CUOTA = (
    "Actúa como tipster colombiano parcero firme y directo. Cuando el usuario te dé una cuota/mercado, "
    "calcula Probabilidad Implícita y EV internamente sin mostrar fórmulas largas. "
    "Responde AL GRANO en MÁXIMO 2 LÍNEAS: "
    "Línea 1: veredicto con emoji: si EV >5% -> '🟢 ¡Métale con confianza!' si no -> '🔴 ¡Pilas, no bote la plata por ahí!'. "
    "Línea 2: justificación corta + casa recomendada: '📍 Búscala en [BetPlay/Wplay/Codere/Zamba]'."
)

# ==========================================
# 2. TECLADOS INLINE (UI/UX)
# ==========================================
def kb_proactivo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Ver Historial", callback_data="ver_historial"),
            InlineKeyboardButton(text="💰 Consultar Banca", callback_data="consultar_banca")
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

# ==========================================
# 3. CAPA DE DATOS (SQLite)
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            telegram_id INTEGER PRIMARY KEY,
            banca_actual REAL DEFAULT 0
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
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN banca_actual REAL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
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
    cursor.execute("INSERT OR REPLACE INTO usuarios (telegram_id, banca_actual) VALUES (?, ?)", (telegram_id, monto))
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
        return "📊 *Historial vacío, parcero.*\nAún no has evaluado cuotas. Envíame un enlace y luego mándame tu cuota."
    calificados = acertados + fallidos
    efectividad = (acertados / calificados * 100) if calificados > 0 else 0.0
    líneas = []
    for _id, partido, mercado, cuota, estado, fecha in rows:
        emoji = "⏳" if estado == "PENDIENTE" else "🟢" if estado == "ACERTADO" else "🔴"
        estado_txt = "Pendiente" if estado == "PENDIENTE" else "Acertado" if estado == "ACERTADO" else "Fallido"
        partido_corto = (partido[:45] + "…") if len(partido) > 45 else partido
        cuota_txt = f"{cuota:.2f}" if cuota else "—"
        líneas.append(f"{emoji} `#{_id}` {partido_corto}\n   {mercado} @ {cuota_txt} — {estado_txt}")
    historial_txt = "\n\n".join(líneas)
    return (
        f"📊 *Tu Historial (últimas 5)*\n\n"
        f"{historial_txt}\n\n"
        f"━━━━━━━━━━━━━━━\n"
        f"📈 *Efectividad:* {efectividad:.1f}% ({acertados}✅/{calificados} calificadas)\n"
        f"📦 Total: {total} | ⏳ Pendientes: {pendientes}"
    )

def get_banca_text(telegram_id: int) -> str:
    banca = get_banca(telegram_id)
    if banca > 0:
        return (
            f"💰 Tu banca actual: {format_pesos(banca)}\n"
            f"Stake 2% = {format_pesos(banca*0.02)} | 3% = {format_pesos(banca*0.03)}\n\n"
            f"Para actualizar: `/banca 105537`"
        )
    return "💰 No has configurado banca, parcero.\nUsa `/banca 105537` para calcular tu stake automático."

# ==========================================
# 4. MÁQUINA DE ESTADOS (FSM)
# ==========================================
class AnalysisStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_quota_or_chat = State()

router = Router()

# ==========================================
# 5. HANDLERS - aiogram v3
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.set_state(AnalysisStates.waiting_for_link)
    await message.answer(
        "👋 *¡Bienvenido al Tipster Bot Pro, mi hermano!*\n\n"
        "📎 Envíame un enlace de Flashscore o FotMob de cualquier partido y te entrego las mejores Value Bets al instante.",
        parse_mode="Markdown",
        reply_markup=kb_proactivo()
    )

@router.message(Command("banca"))
async def cmd_banca(message: Message):
    telegram_id = message.from_user.id
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

@router.message(Command("historial"))
async def cmd_historial(message: Message):
    await message.answer(get_historial_text(message.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "ver_historial")
async def cb_ver_historial(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_historial_text(callback.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "consultar_banca")
async def cb_consultar_banca(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_banca_text(callback.from_user.id), parse_mode="Markdown")

@router.message(F.text & ~F.text.startswith("/"))
async def handle_user_flow(message: Message, state: FSMContext):
    current_state = await state.get_state()
    telegram_id = message.from_user.id
    text = message.text.strip()
    text_lower = text.lower()

    if "flashscore" in text_lower or "fotmob" in text_lower:
        status_msg = await message.answer("🔍 Enlace válido. Extrayendo datos...")
        try:
            scraped_text, _, _ = await fetch_match_data(text)
            if not scraped_text or len(scraped_text.strip()) < 80:
                await status_msg.edit_text("❌ No se pudo leer el partido, intenta de nuevo.")
                return
        except Exception:
            await status_msg.edit_text("❌ Error al extraer datos del partido.")
            return

        await state.update_data(scraped_text=scraped_text, last_url=text)
        await status_msg.edit_text("📊 Analizando con Gemini...")
        try:
            prompt = f"Datos del partido:\n\n{scraped_text}"
            analysis_result = await gemini_generate_with_retry(SYSTEM_INSTRUCTION, prompt)
            await message.answer(analysis_result, reply_markup=kb_cuota())
            await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
            await status_msg.delete()
        except Exception as e:
            logging.error(f'Gemini API Error: {e}')
            await status_msg.edit_text("❌ Error al conectar con Gemini. Intenta de nuevo, parcero.")
        return

    if current_state == AnalysisStates.waiting_for_quota_or_chat.state:
        data = await state.get_data()
        scraped_text = data.get("scraped_text", "")
        last_url = data.get("last_url", "partido")
        if not scraped_text:
            await message.answer("⚠️ Envía primero un enlace de partido.")
            await state.set_state(AnalysisStates.waiting_for_link)
            return

        processing_msg = await message.answer("🤖 Evaluando tu cuota...")
        try:
            prompt = f"Contexto:\n{scraped_text}\n\nCuota elegida:\n{text}"
            result = await gemini_generate_with_retry(SYSTEM_INSTRUCTION_CUOTA, prompt)
            
            cuota_val = 0.0
            try:
                match = re.search(r"(\d+[.,]\d+)", text)
                if match:
                    cuota_val = float(match.group(1).replace(",", "."))
            except Exception:
                pass

            fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO historial_apuestas (telegram_id, partido, mercado, cuota, estado, fecha) VALUES (?, ?, ?, ?, 'PENDIENTE', ?)", 
                           (telegram_id, last_url[:300], text[:120], cuota_val, fecha_now))
            conn.commit()
            hid = cursor.lastrowid
            conn.close()

            banca = get_banca(telegram_id)
            stake_msg = f"\n\n📝 Guardado en historial `#{hid}` ⏳"
            if banca > 0:
                stake_msg += f"\n💰 Sugerido Stake (3%): {format_pesos(banca*0.03)}"

            await processing_msg.edit_text(result + stake_msg, parse_mode="Markdown")
            await message.answer("¿Qué otro partido analizamos, parcero? Envíame otro enlace.", reply_markup=kb_proactivo())
            await state.set_state(AnalysisStates.waiting_for_link)
        except Exception as e:
            logging.error(f'Gemini API Error al evaluar cuota: {e}')
            await processing_msg.edit_text("❌ Error al evaluar la cuota con Gemini.")
        return

    await message.answer("⚠️ Envía un enlace válido de Flashscore o FotMob para empezar.", reply_markup=kb_proactivo())
    await state.set_state(AnalysisStates.waiting_for_link)

# ==========================================
# 6. WEBHOOKS & FLASK
# ==========================================
web_app = Flask(__name__)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

@web_app.route('/')
def home():
    return '¡El Bot de Apuestas con Webhook y Gemini está activo 24/7, mi hermano! 🚀⚽'

def run_async_update(update_json):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    update = Update.model_validate(update_json, context={"bot": bot})
    loop.run_until_complete(dp.feed_update(bot, update))
    loop.close()

@web_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json()
        threading.Thread(target=run_async_update, args=(json_data,), daemon=True).start()
        return "", 200
    return "Invalid request", 403

def setup_webhook():
    url = f"{WEBHOOK_URL.rstrip('/')}/webhook/{TELEGRAM_TOKEN}"
    requests_sync = req_requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={url}")
    logging.info(f"Configurando Webhook en Telegram: {requests_sync.text}")

if __name__ == '__main__':
    init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    setup_webhook()
    port = int(os.getenv('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
