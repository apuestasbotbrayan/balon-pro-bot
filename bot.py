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
import time
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, Update
from flask import Flask, request

from google import genai
from playwright.async_api import async_playwright
from playwright_stealth import stealth_async

# Importaciones para Google Sheets
import gspread
from google.oauth2.service_account import Credentials

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
                f"Estado: {estado_txt} | Minuto actual: {minute} | Marcador: {score}",
                f"Contexto temporal: {'Partido pre-partido' if estado_txt == 'Pre-partido' else f'ATENCIÓN EN VIVO: Minuto {minute} con marcador {score}. Adapta los mercados de riesgo.'}"
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
# 1. CONFIGURACIÓN E INTELIGENCIA ARTIFICIAL
# ==========================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8785828541:AAHuZoLPpmwDYXzXl92b_PxMDxJ3jpY0Q6g")
RAW_KEYS = os.getenv("GEMINI_API_KEY", "")
API_KEYS_LIST = [k.strip() for k in RAW_KEYS.split(",") if k.strip()]
current_key_index = 0

ADMIN_ID = int(os.getenv("ADMIN_ID", "8021280020"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "https://balon-pro-bot.onrender.com")
DB_NAME = "bot_database.db"
GEMINI_MODEL_ID = "gemini-3.6-flash"

def get_next_ai_client():
    global current_key_index
    if not API_KEYS_LIST:
        return genai.Client(api_key=os.getenv("GEMINI_API_KEY", ""))
    key = API_KEYS_LIST[current_key_index]
    current_key_index = (current_key_index + 1) % len(API_KEYS_LIST)
    return genai.Client(api_key=key)

async def gemini_generate_with_retry(system_instruction: str, user_prompt: str, max_retries: int = 3):
    full_prompt = f"{system_instruction}\n\n{user_prompt}"
    for attempt in range(max_retries + 1):
        try:
            client = get_next_ai_client()
            response = await asyncio.to_thread(
                client.models.generate_content,
                model=GEMINI_MODEL_ID,
                contents=full_prompt,
            )
            return response.text.strip()
        except Exception as e:
            err_str = str(e)
            logging.warning(f"Intento {attempt + 1} falló: {err_str}")
            if ("429" in err_str or "RESOURCE_EXHAUSTED" in err_str) and attempt < max_retries:
                await asyncio.sleep(1)
                continue
            if attempt < max_retries:
                await asyncio.sleep(2)
                continue
            raise

SYSTEM_INSTRUCTION = (
    "Actúa como tipster profesional colombiano parcero experto. "
    "MUY IMPORTANTE: Analiza con lupa el MINUTO ACTUAL y el MARCADOR del partido en los datos. "
    "Si el partido está avanzado (ej: minuto 75+), tus Value Bets deben ser de ALTO RIESGO EN VIVO. "
    "SÉ ULTRA RESUMIDO y VISUAL, sin floro. "
    "Saluda breve parcero ('¡Epa, mi hermano!') y presenta de una 3 Value Bets SÓLIDAS Y ACORDES AL TIEMPO ACTUAL. "
    "PROHIBIDO poner números de cuota falsos. "
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
# 2. TECLADOS INLINE
# ==========================================
def kb_proactivo() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="📊 Ver Historial", callback_data="ver_historial"),
            InlineKeyboardButton(text="💰 Consultar Banca", callback_data="consultar_banca")
        ]
    ])

def kb_pago_requerido() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="💬 Escríbeme al WhatsApp", url="https://wa.me/573216880439?text=Hola,%20quiero%20activar%20mi%20cuenta%20VIP%20de%20Balon%20Pro%20por%20$10.000")
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

def kb_calificar_apuesta(apuesta_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Acertada", callback_data=f"est_ok_{apuesta_id}"),
            InlineKeyboardButton(text="❌ Fallida", callback_data=f"est_fail_{apuesta_id}")
        ]
    ])

# ==========================================
# 3. CAPA DE DATOS (SQLite, Google Sheets & Códigos)
# ==========================================
SCOPES = ["https://www.spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]

def registrar_en_google_sheets(telegram_id: int, banca: float, estado: str):
    try:
        if not os.path.exists("credentials.json"):
            return
        creds = Credentials.from_service_account_file("credentials.json", scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open("BalonPro_Registros").sheet1
        
        try:
            cell = sheet.find(str(telegram_id))
            if cell:
                sheet.update_cell(cell.row, 2, banca)
                sheet.update_cell(cell.row, 3, estado)
                sheet.update_cell(cell.row, 4, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
                return
        except Exception:
            pass
        
        sheet.append_row([str(telegram_id), banca, estado, datetime.now().strftime("%Y-%m-%d %H:%M:%S")])
    except Exception as e:
        logging.error(f"Error sincronizando con Google Sheets: {e}")

def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS usuarios (
            telegram_id INTEGER PRIMARY KEY,
            banca_actual REAL DEFAULT 0,
            activo INTEGER DEFAULT 0,
            fecha_expiracion TEXT,
            fecha_registro TEXT
        )
    """)
    try:
        cursor.execute("ALTER TABLE usuarios ADD COLUMN fecha_expiracion TEXT")
    except sqlite3.OperationalError:
        pass

    # Tabla para almacenar códigos de activación (Pines)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS codigos (
            codigo TEXT PRIMARY KEY,
            duracion TEXT,
            usado INTEGER DEFAULT 0,
            fecha_creacion TEXT
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS historial_apuestas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_id INTEGER,
            partido TEXT,
            mercado TEXT,
            cuota REAL,
            stake REAL DEFAULT 0,
            estado TEXT DEFAULT 'PENDIENTE',
            fecha TEXT
        )
    """)
    conn.commit()
    conn.close()

def verificar_vigencia_usuario(telegram_id: int) -> int:
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT activo, fecha_expiracion, banca_actual FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    
    if row is None:
        fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("INSERT INTO usuarios (telegram_id, banca_actual, activo, fecha_registro) VALUES (?, 0, 0, ?)", (telegram_id, fecha_now))
        conn.commit()
        conn.close()
        threading.Thread(target=registrar_en_google_sheets, args=(telegram_id, 0.0, "INACTIVO"), daemon=True).start()
        return 0
    
    activo, fecha_exp, banca = row[0], row[1], row[2]
    conn.close()

    if activo == 1 and fecha_exp:
        try:
            exp_dt = datetime.strptime(fecha_exp, "%Y-%m-%d %H:%M:%S")
            if datetime.now() > exp_dt:
                conn = sqlite3.connect(DB_NAME)
                cursor = conn.cursor()
                cursor.execute("UPDATE usuarios SET activo = 0 WHERE telegram_id = ?", (telegram_id,))
                conn.commit()
                conn.close()
                threading.Thread(target=registrar_en_google_sheets, args=(telegram_id, banca, "VENCIDO"), daemon=True).start()
                return 0
        except Exception:
            pass
            
    estado_str = f"ACTIVO HASTA {fecha_exp}" if activo == 1 and fecha_exp else "INACTIVO"
    threading.Thread(target=registrar_en_google_sheets, args=(telegram_id, banca if banca else 0.0, estado_str), daemon=True).start()
    return activo

def activar_usuario_tiempo_str(telegram_id: int, duracion_str: str = "1m"):
    ahora = datetime.now()
    cantidad = int("".join(filter(str.isdigit, duracion_str))) or 1
    unidad = "".join(filter(str.isalpha, duracion_str)).lower()

    if "d" in unidad:
        expiracion = ahora + timedelta(days=cantidad)
    elif "a" in unidad:
        expiracion = ahora + timedelta(days=cantidad * 365)
    else:
        expiracion = ahora + timedelta(days=cantidad * 30)

    expiracion_str = expiracion.strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        UPDATE usuarios 
        SET activo = 1, fecha_expiracion = ? 
        WHERE telegram_id = ?
    """, (expiracion_str, telegram_id))
    conn.commit()
    
    cursor.execute("SELECT banca_actual FROM usuarios WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    banca = row[0] if row else 0.0
    conn.close()
    
    threading.Thread(target=registrar_en_google_sheets, args=(telegram_id, banca, f"ACTIVO HASTA {expiracion_str}"), daemon=True).start()
    return expiracion_str

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
    verificar_vigencia_usuario(telegram_id)

def descontar_banca(telegram_id: int, monto_stake: float):
    banca_actual = get_banca(telegram_id)
    if banca_actual > 0:
        nueva_banca = max(0.0, banca_actual - monto_stake)
        set_banca(telegram_id, nueva_banca)

def format_pesos(valor: float) -> str:
    return f"$ {int(round(valor)):,.0f}".replace(",", ".")

def actualizar_estado_apuesta(apuesta_id: int, nuevo_estado: str):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("UPDATE historial_apuestas SET estado = ? WHERE id = ?", (nuevo_estado, apuesta_id))
    conn.commit()
    conn.close()

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
        return "📊 *Historial vacío, parcero.*\nAún no has evaluado cuotas."
    calificados = acertados + fallidos
    efectividad = (acertados / calificados * 100) if calificados > 0 else 0.0
    líneas = []
    for _id, partido, mercado, cuota, estado, fecha in rows:
        emoji = "⏳" if estado == "PENDIENTE" else "🟢" if estado == "ACERTADO" else "🔴"
        estado_txt = "Pendiente" if estado == "PENDIENTE" else "Acertado" if estado == "ACERTADO" else "Fallido"
        partido_corto = (partido[:40] + "…") if len(partido) > 40 else partido
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
            f"Para actualizar: `/banca 100000`"
        )
    return "💰 No has configurado banca, parcero.\nUsa `/banca 100000` para calcular tu stake automático."

# ==========================================
# 4. MÁQUINA DE ESTADOS (FSM)
# ==========================================
class AnalysisStates(StatesGroup):
    waiting_for_link = State()
    waiting_for_quota_or_chat = State()

router = Router()

# ==========================================
# 5. HANDLERS
# ==========================================
@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    activo = verificar_vigencia_usuario(telegram_id)

    if activo == 0 and telegram_id != ADMIN_ID:
        await message.answer(
            "🛑 *¡Acceso Restringido o Suscripción Vencida, mi hermano!*\n\n"
            "El acceso mensual a **Balon Pro AI** cuesta solo **$ 10.000 COP**.\n\n"
            "💬 Escríbeme al WhatsApp **3216880439** para reportar tu pago y recibir tu código de canje.\n"
            "Si ya tienes un código, actívalo enviando: `/canje [tu_codigo]`",
            parse_mode="Markdown",
            reply_markup=kb_pago_requerido()
        )
        return

    await state.set_state(AnalysisStates.waiting_for_link)
    await message.answer(
        "👋 *¡Bienvenido al Tipster Bot Pro, mi hermano!*\n\n"
        "📎 Envíame un enlace de Flashscore o FotMob (Pre-partido o En Vivo), o una foto de tu apuesta, y te entrego las mejores Value Bets al instante.",
        parse_mode="Markdown",
        reply_markup=kb_proactivo()
    )

# Comando de Admin para generar códigos cortos listos para copiar y pegar: /generar 1m (o 7d, 1a)
@router.message(Command("generar"))
async def cmd_generar(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    args = message.text.split()
    duracion = args[1].strip() if len(args) > 1 else "1m"
    
    # Generar un código corto tipo BP-XXXX (4 caracteres aleatorios en mayúsculas y números)
    sufijo = ''.join(random.choices(string.ascii_uppercase + string.digits, k=4))
    codigo = f"BP-{sufijo}"
    fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT INTO codigos (codigo, duracion, usado, fecha_creacion) VALUES (?, ?, 0, ?)", (codigo, duracion, fecha_now))
    conn.commit()
    conn.close()

    texto_copiar = (
        f"🎟️ *¡Código VIP Generado!*\n\n"
        f"Duración: `{duracion}`\n"
        f"Código de activación:\n`{codigo}`\n\n"
        f"📋 *Mensaje listo para enviar al cliente:*\n"
        f"━━━━━━━━━━━━━━━\n"
        f"¡Hola, mi hermano! ⚽ Recibido tu pago de $10.000 COP. Canjea este código en el bot de Telegram escribiendo exactamente:\n\n"
        f"`/canje {codigo}`\n\n"
        f"¡A facturar con Balon Pro AI! 🚀"
    )
    await message.answer(texto_copiar, parse_mode="Markdown")

# Comando para que el cliente canjee su código
@router.message(Command("canje"))
async def cmd_canje(message: Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Uso incorrecto. Debes enviar el código así:\n`/canje BP-XXXX`", parse_mode="Markdown")
        return
    
    codigo_ingresado = args[1].strip().upper()
    telegram_id = message.from_user.id

    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT duracion, usado FROM codigos WHERE codigo = ?", (codigo_ingresado,))
    row = cursor.fetchone()

    if row is None:
        conn.close()
        await message.answer("❌ *Código inválido.* Verifica que lo hayas escrito bien, parcero.", parse_mode="Markdown")
        return
    
    duracion, usado = row[0], row[1]
    if usado == 1:
        conn.close()
        await message.answer("⚠️ *Este código ya fue utilizado.* Los códigos son de un solo uso.", parse_mode="Markdown")
        return

    # Marcar código como usado
    cursor.execute("UPDATE codigos SET usado = 1 WHERE codigo = ?", (codigo_ingresado,))
    conn.commit()
    conn.close()

    # Activar la suscripción al usuario
    exp_str = activar_usuario_tiempo_str(telegram_id, duracion)

    await message.answer(
        f"🎉 *¡Código canjeado con éxito!*\n\n"
        f"Tu cuenta ha sido activada por `{duracion}` (Hasta: {exp_str}).\n"
        f"Envía `/start` para comenzar a analizar partidos. ¡A facturar, parcero! 🚀⚽",
        parse_mode="Markdown",
        reply_markup=kb_proactivo()
    )

@router.message(Command("banca"))
async def cmd_banca(message: Message):
    telegram_id = message.from_user.id
    if verificar_vigencia_usuario(telegram_id) == 0 and telegram_id != ADMIN_ID:
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
            f"✅ Banca actualizada: {format_pesos(monto)}\n"
            f"📊 Stake prudente → 2% = {format_pesos(monto*0.02)} | 3% = {format_pesos(monto*0.03)}",
            reply_markup=kb_proactivo()
        )
    except ValueError:
        await message.answer("❌ Monto inválido. Ej: `/banca 100000`")

@router.message(Command("historial"))
async def cmd_historial(message: Message):
    telegram_id = message.from_user.id
    if verificar_vigencia_usuario(telegram_id) == 0 and telegram_id != ADMIN_ID:
        return
    await message.answer(get_historial_text(telegram_id), parse_mode="Markdown")

@router.callback_query(F.data == "ver_historial")
async def cb_ver_historial(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_historial_text(callback.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data == "consultar_banca")
async def cb_consultar_banca(callback: CallbackQuery):
    await callback.answer()
    await callback.message.answer(get_banca_text(callback.from_user.id), parse_mode="Markdown")

@router.callback_query(F.data.startswith("est_"))
async def cb_calificar(callback: CallbackQuery):
    parts = callback.data.split("_")
    if len(parts) == 3:
        accion = parts[1]
        apuesta_id = int(parts[2])
        nuevo_estado = "ACERTADO" if accion == "ok" else "FALLIDO"
        actualizar_estado_apuesta(apuesta_id, nuevo_estado)
        await callback.answer(f"¡Apuesta #{apuesta_id} marcada como {nuevo_estado}!")
        await callback.message.edit_text(
            f"✅ *Apuesta `#{apuesta_id}` actualizada con éxito!*\n\n" + get_historial_text(callback.from_user.id),
            parse_mode="Markdown"
        )
    else:
        await callback.answer("Acción no válida.")

@router.message(F.photo)
async def handle_photo_flow(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    if verificar_vigencia_usuario(telegram_id) == 0 and telegram_id != ADMIN_ID:
        return
    status_msg = await message.answer("📸 Imagen recibida. Analizando ticket o apuesta...")
    try:
        photo = message.photo[-1]
        file_info = await bot.get_file(photo.file_id)
        file_bytes = await bot.download_file(file_info.file_path)
        
        client = get_next_ai_client()
        prompt_imagen = (
            "Actúa como tipster profesional colombiano. Analiza esta captura de pantalla de apuestas o partido. "
            "Extrae los datos clave y da tu veredicto rápido de valor en máximo 3 líneas."
        )
        
        response = await asyncio.to_thread(
            client.models.generate_content,
            model=GEMINI_MODEL_ID,
            contents=[
                prompt_imagen,
                {"mime_type": "image/jpeg", "data": file_bytes.read()}
            ]
        )
        
        await status_msg.edit_text(response.text.strip(), parse_mode="Markdown", reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_link)
    except Exception as e:
        logging.error(f"Error leyendo imagen: {e}")
        await status_msg.edit_text("❌ No pude leer bien la imagen, parcero. Intenta de nuevo o pásame el enlace.")

@router.message(F.text & ~F.text.startswith("/"))
async def handle_user_flow(message: Message, state: FSMContext):
    telegram_id = message.from_user.id
    if verificar_vigencia_usuario(telegram_id) == 0 and telegram_id != ADMIN_ID:
        await message.answer(
            "🛑 *¡Acceso Restringido o Vencido!*\nEscríbeme al WhatsApp **3216880439** para adquirir tu código o canjealo con `/canje [codigo]`.",
            parse_mode="Markdown",
            reply_markup=kb_pago_requerido()
        )
        return

    text = message.text.strip()
    text_lower = text.lower()

    if "flashscore" in text_lower or "fotmob" in text_lower:
        status_msg = await message.answer("🔍 Enlace válido. Extrayendo datos (Pre-partido / En vivo)...")
        try:
            scraped_text, _, _ = await fetch_match_data(text)
            if not scraped_text or len(scraped_text.strip()) < 80:
                await status_msg.edit_text("❌ No se pudo leer el partido, intenta de nuevo.")
                return
        except Exception:
            await status_msg.edit_text("❌ Error al extraer datos del partido.")
            return

        await state.update_data(scraped_text=scraped_text, last_url=text)
        await status_msg.edit_text("📊 Analizando contexto, tiempo y estadísticas...")
        try:
            prompt = f"Datos del partido:\n\n{scraped_text}"
            analysis_result = await gemini_generate_with_retry(SYSTEM_INSTRUCTION, prompt)
            await message.answer(analysis_result, reply_markup=kb_cuota())
            await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
            await status_msg.delete()
        except Exception as e:
            logging.error(f'API Error: {e}')
            await status_msg.edit_text("❌ Error al procesar el partido. Intenta de nuevo, parcero.")
        return

    data = await state.get_data()
    scraped_text = data.get("scraped_text", "")
    last_url = data.get("last_url", "partido")

    if scraped_text:
        tiene_numero = re.search(r"\d+[.,]\d+", text)
        
        if not tiene_numero and ("otra" in text_lower or "opcion" in text_lower or "opción" in text_lower or "mas" in text_lower or "más" in text_lower):
            refresh_msg = await message.answer("🔄 Buscando opciones tácticas diferentes...")
            try:
                prompt = f"Datos del partido:\n\n{scraped_text}\n\nGenera 3 mercados COMPLETAMENTE DIFERENTES adaptados al estado actual, ultra resumidos."
                alt_result = await gemini_generate_with_retry(SYSTEM_INSTRUCTION, prompt)
                await refresh_msg.edit_text(alt_result, reply_markup=kb_cuota(), parse_mode="Markdown")
            except Exception as e:
                logging.error(f'Error generando opciones alternativas: {e}')
                await refresh_msg.edit_text("❌ No pude generar más opciones en este momento, parcero.")
            return

        processing_msg = await message.answer("🤖 Evaluando tu cuota y stake...")
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

            banca = get_banca(telegram_id)
            stake_monto = banca * 0.03 if banca > 0 else 0.0

            if stake_monto > 0:
                descontar_banca(telegram_id, stake_monto)

            fecha_now = datetime.now().strftime("%Y-%m-%d %H:%M")
            conn = sqlite3.connect(DB_NAME)
            cursor = conn.cursor()
            cursor.execute("INSERT INTO historial_apuestas (telegram_id, partido, mercado, cuota, stake, estado, fecha) VALUES (?, ?, ?, ?, ?, 'PENDIENTE', ?)", 
                           (telegram_id, last_url[:300], text[:120], cuota_val, stake_monto, fecha_now))
            conn.commit()
            hid = cursor.lastrowid
            conn.close()

            banca_nueva = get_banca(telegram_id)
            stake_msg = f"\n\n📝 Guardado en historial `#{hid}` ⏳"
            if stake_monto > 0:
                stake_msg += f"\n💰 Stake apostado (3%): {format_pesos(stake_monto)}\n📉 Nueva Banca: {format_pesos(banca_nueva)}"

            await processing_msg.edit_text(result + stake_msg, parse_mode="Markdown", reply_markup=kb_calificar_apuesta(hid))
            await message.answer("¿Qué otro partido analizamos, parcero? Envíame otro enlace.", reply_markup=kb_proactivo())
            await state.set_state(AnalysisStates.waiting_for_link)
        except Exception as e:
            logging.error(f'API Error al evaluar cuota: {e}')
            await processing_msg.edit_text("❌ Error al evaluar la cuota.")
        return

    await message.answer("⚠️ Envía un enlace válido de Flashscore o FotMob para empezar.", reply_markup=kb_proactivo())
    await state.set_state(AnalysisStates.waiting_for_link)

# ==========================================
# 6. WEBHOOKS, FLASK & KEEP-ALIVE PING
# ==========================================
web_app = Flask(__name__)
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=MemoryStorage())
dp.include_router(router)

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

@web_app.route('/')
def home():
    return '¡El Bot Pro está activo 24/7, mi hermano! 🚀⚽'

@web_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def telegram_webhook():
    if request.headers.get("content-type") == "application/json":
        json_data = request.get_json()
        update = Update.model_validate(json_data, context={"bot": bot})
        future = asyncio.run_coroutine_threadsafe(dp.feed_update(bot, update), loop)
        try:
            future.result(timeout=25)
        except Exception as e:
            logging.error(f"Error procesando update en webhook: {e}")
        return "", 200
    return "Invalid request", 403

def setup_webhook():
    url = f"{WEBHOOK_URL.rstrip('/')}/webhook/{TELEGRAM_TOKEN}"
    requests_sync = req_requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook?url={url}")
    logging.info(f"Configurando Webhook en Telegram: {requests_sync.text}")

def start_background_loop(ioloop):
    asyncio.set_event_loop(ioloop)
    ioloop.run_forever()

def keep_alive_ping():
    url = WEBHOOK_URL.rstrip('/') + '/'
    while True:
        try:
            time.sleep(420)
            resp = req_requests.get(url, timeout=10)
            logging.info(f"Ping Keep-Alive enviado a Render. Status: {resp.status_code}")
        except Exception as e:
            logging.warning(f"Error en el ping Keep-Alive: {e}")

if __name__ == '__main__':
    init_db()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
    setup_webhook()
    
    t = threading.Thread(target=start_background_loop, args=(loop,), daemon=True)
    t.start()
    
    ping_thread = threading.Thread(target=keep_alive_ping, daemon=True)
    ping_thread.start()
    
    port = int(os.getenv('PORT', 10000))
    web_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
