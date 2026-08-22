import asyncio
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

# === PROMPT PROACTIVO CORREGIDO - SIN INVENTAR CUOTAS ===
SYSTEM_INSTRUCTION = (
    "Actúa como tipster profesional colombiano parcero experto. Cuando recibas datos de Flashscore, "
    "REVISA CON LUPA las alineaciones probables y bajas de jugadores clave, además de árbitro, H2H y tendencias. "
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
        "Envíame varios enlaces de Flashscore uno por uno (Partido 1, Partido 2...).\n"
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
    status_msg = await message.answer("🔍 Enlace de partido en vivo seleccionado, parcero. Extrayendo con Playwright...", reply_markup=kb_proactivo())
    scraped_text = ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 800}, locale="es-CO", extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"}, java_script_enabled=True)
            page = await context.new_page()
            await asyncio.sleep(random.uniform(1.0, 2.0))
            await page.goto(url, timeout=60000, wait_until="commit")
            await asyncio.sleep(random.uniform(2.0, 3.5))
            try:
                await page.wait_for_selector("body", timeout=8000)
            except Exception:
                pass
            body_text = await page.inner_text("body")
            scraped_text = body_text[:3000]
            await browser.close()
    except Exception as e:
        logging.exception(f"Error Playwright hoy: {e}")
        await status_msg.edit_text("❌ Error al extraer datos del partido en vivo. Intenta con otro enlace, mi hermano.")
        return
    if not scraped_text.strip():
        await status_msg.edit_text("❌ No se pudo extraer contenido del partido.")
        return
    await state.update_data(scraped_text=scraped_text, last_url=url)
    await status_msg.edit_text("📊 Estadísticas leídas. Analizando con lupa alineaciones y bajas...")
    try:
        model = genai.GenerativeModel(model_name="gemini-3.6-flash", system_instruction=SYSTEM_INSTRUCTION)
        prompt = f"Datos extraídos de Flashscore (H2H, alineaciones probables, bajas de jugadores clave, árbitro, tendencias) - Partido: {url}\n\n{scraped_text}"
        response = await asyncio.to_thread(model.generate_content, prompt)
        analysis_result = response.text.strip() if hasattr(response, "text") and response.text else str(response)
        increment_user_usage(telegram_id)
        await message.answer(analysis_result, reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
        await status_msg.delete()
    except Exception as e:
        logging.exception(f"Error Gemini hoy: {e}")
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
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 800}, locale="es-CO", extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"}, java_script_enabled=True)
            page = await context.new_page()
            await asyncio.sleep(random.uniform(1.0, 2.0))
            await page.goto("https://www.flashscore.co/", timeout=60000, wait_until="commit")
            await asyncio.sleep(random.uniform(4.0, 6.0))
            try:
                await page.wait_for_selector('.event__match, [id^="g_1"], .sportName', timeout=12000)
            except Exception:
                try:
                    await page.wait_for_selector("body", timeout=5000)
                except Exception:
                    pass
            # Extraer TODOS los partidos del día con lógica de VALOR/RELEVANCIA
            try:
                partidos = await page.evaluate("""() => {
                    const seen = new Set();
                    const raw = [];

                    const getLeagueScore = (text) => {
                        const t = (text || '').toUpperCase();
                        if (/PRIMERA A|BETPLAY|DIMAYOR|COLOMBIA.*PRIMERA/.test(t)) return 100;
                        if (/PREMIER LEAGUE|LA LIGA|LALIGA|SERIE A|BUNDESLIGA|LIGUE 1|CHAMPIONS|LIBERTADORES|EUROPA LEAGUE|BRASILEIRAO|SUDAMERICANA|LIGA MX|CLASICO|DERBY/.test(t)) return 95;
                        if (/PRIMERA B|COPA LIBERTADORES|COPA SUDAMERICANA|EREDIVISIE|PRIMEIRA LIGA|CHAMPIONSHIP|MLS|ARGENTINA.*PRIMERA/.test(t)) return 70;
                        if (/AMISTOSO|FRIENDLY|RESERVA|SUB-?19|SUB-?20|FEMENINO|TERCERA|SEGUNDA B|REGIONAL/.test(t)) return 10;
                        return 35;
                    };

                    const getLeagueText = (container) => {
                        let parent = container.parentElement;
                        let tries = 0;
                        while (parent && tries < 6) {
                            const header = parent.querySelector('.event__header, .sportName');
                            if (header && header.innerText.trim().length > 3) return header.innerText.trim();
                            // Buscar hermano previo
                            let prev = container.previousElementSibling;
                            let pt = 0;
                            while (prev && pt < 4) {
                                if (prev.matches && prev.matches('.event__header, .sportName')) return prev.innerText.trim();
                                prev = prev.previousElementSibling; pt++;
                            }
                            parent = parent.parentElement;
                            tries++;
                        }
                        return '';
                    };

                    // Capturar TODOS: en vivo + programados de la jornada
                    const containers = Array.from(document.querySelectorAll('.event__match, .event__game, [id^="g_1"] div.event__match, li.event__match'));
                    const anchorsFallback = Array.from(document.querySelectorAll('a[href*="/partido/"], a[href*="/match/"]'));

                    const processContainer = (container, hrefOverride) => {
                        let href = hrefOverride || null;
                        if (!href) {
                            const a = container.querySelector('a[href*="/partido/"], a[href*="/match/"], a');
                            href = a ? a.href : null;
                        }
                        if (!href || seen.has(href)) return;
                        if (!href.includes('/partido/') && !href.includes('/match/')) return;
                        seen.add(href);

                        const timeEl = container.querySelector('.event__time, .event__stage, [class*="event__time"]');
                        const scoreEl = container.querySelector('.event__score, .event__scores, [class*="event__score"]');
                        const participantEls = container.querySelectorAll('.event__participant, .event__participant--home, .event__participant--away, [class*="participant"]');

                        let timeText = timeEl ? timeEl.innerText.trim() : '';
                        let scoreText = scoreEl ? scoreEl.innerText.trim().replace(/\\s+/g,' ') : '';

                        let containerText = container.innerText.replace(/\\s+/g,' ').trim();
                        if (!timeText) {
                            let m = containerText.match(/\\b\\d{1,3}(?:\\+\\d+)?'?\\b/);
                            if (m) timeText = m[0];
                        }
                        if (!scoreText) {
                            let sm = containerText.match(/\\b\\d+\\s*[-:]\\s*\\d+\\b/);
                            if (sm) scoreText = sm[0];
                        }

                        let minute = null;
                        let time = null;
                        if (/^\\d{1,3}(\\+\\d+)?'?$/.test(timeText.trim())) {
                            minute = timeText.trim();
                            if (!minute.includes("'")) minute = minute + "'";
                        } else if (/^\\d{1,2}:\\d{2}$/.test(timeText.trim())) {
                            time = timeText.trim();
                        } else {
                            let minuteMatch = containerText.match(/(\\d{1,3}(?:\\+\\d+)?')/);
                            if (minuteMatch) minute = minuteMatch[1];
                            else {
                                let minuteNoApos = containerText.match(/\\b(\\d{1,3}(?:\\+\\d+)?)\\b/);
                                if (minuteNoApos && parseInt(minuteNoApos[1]) <= 130 && !containerText.includes(':')) {
                                    if (scoreText) minute = minuteNoApos[1] + "'";
                                }
                            }
                            let timeMatch = containerText.match(/\\b(\\d{1,2}:\\d{2})\\b/);
                            if (timeMatch) time = timeMatch[1];
                        }

                        let score = null;
                        if (scoreText) {
                            let s = scoreText.match(/(\\d+)\\s*[-:]\\s*(\\d+)/);
                            if (s) score = `${s[1]}-${s[2]}`;
                        }
                        if (!score) {
                            let sm = containerText.match(/(\\d+)\\s*[-:]\\s*(\\d+)/);
                            if (sm) score = `${sm[1]}-${sm[2]}`;
                        }

                        let teams = '';
                        if (participantEls.length >= 2) {
                            teams = Array.from(participantEls).slice(0,2).map(e=>e.innerText.trim()).filter(Boolean).join(' vs ');
                        }
                        if (!teams || teams.length < 3) {
                            const a = container.querySelector('a[href*="/partido/"]');
                            teams = a ? (a.innerText.trim() || a.textContent.trim()) : '';
                        }
                        if (!teams || teams.length < 3) {
                            teams = containerText.replace(minute||'','').replace(score||'','').replace(time||'','').replace(/EN VIVO|LIVE/gi,'').trim().substring(0,40);
                            if (!teams) teams = href.split('/').filter(Boolean).pop() || 'Partido';
                        }
                        teams = teams.replace(/\\s+/g,' ').trim();
                        if (teams.length > 32) teams = teams.substring(0,32) + '…';
                        if (teams.length < 3) return;

                        let isLive = !!minute || /en vivo|live|en juego/i.test(containerText) || !!scoreEl;
                        if (score && minute) isLive = true;
                        if (!isLive && (score || minute)) isLive = true;

                        // Lógica de VALOR/RELEVANCIA: prioriza Ligas Pro / Primera A, clásicos y alta expectativa de goles
                        let leagueText = getLeagueText(container);
                        let leagueScore = getLeagueScore(leagueText + ' ' + containerText + ' ' + teams + ' ' + href);
                        if (/NACIONAL|MILLONARIOS|AMERICA.*CALI|JUNIOR|SANT A FE|MEDELLIN|BOCA|RIVER|FLAMENGO|PALMEIRAS|CLASICO|DERBY/.test(teams.toUpperCase())) leagueScore += 8;

                        let display = '';
                        if (isLive && minute && score) {
                            display = `${minute} [${score}] ${teams}`;
                        } else if (isLive && score) {
                            display = `EN VIVO [${score}] ${teams}`;
                        } else if (isLive && minute) {
                            display = `${minute} ${teams}`;
                        } else if (time) {
                            display = `${time} - ${teams}`;
                        } else {
                            display = teams;
                        }

                        let sortKey = 9999;
                        if (isLive) {
                            let m = parseInt(minute) || 0;
                            sortKey = -1000 + (100 - m);
                        } else if (time) {
                            let [h, mi] = time.split(':').map(Number);
                            sortKey = h*60 + mi;
                        }
                        let attractive = (isLive && score && score !== '0-0') ? 1 : 0;
                        raw.push({href, text: display, isLive, time, score, minute, sortKey, attractive, leagueScore, leagueText});
                    };

                    // Capturar TODOS los partidos del día (en vivo + programados) para filtrar por valor
                    for (const c of containers) {
                        processContainer(c);
                        if (raw.length >= 30) break;
                    }
                    if (raw.length < 6) {
                        for (const a of anchorsFallback) {
                            processContainer(a.closest('.event__match') || a.parentElement?.parentElement || a, a.href);
                            if (raw.length >= 30) break;
                        }
                    }

                    // Orden inteligente: en vivo primero (con minuto/marcador), luego por VALOR de liga, atractivo y hora
                    raw.sort((a,b) => {
                        if (a.isLive && !b.isLive) return -1;
                        if (!a.isLive && b.isLive) return 1;
                        if (a.leagueScore !== b.leagueScore) return b.leagueScore - a.leagueScore;
                        if (a.attractive !== b.attractive) return b.attractive - a.attractive;
                        return a.sortKey - b.sortKey;
                    });
                    // Seleccionar los 10-12 más atractivos (prioriza Ligas Pro / Primera A y clásicos)
                    let result = raw.slice(0,12).map(({href,text,isLive})=>({href,text,isLive}));
                    if (result.length === 0) {
                        // Último fallback: cualquier a en g_1
                        const fallback = Array.from(document.querySelectorAll('[id^="g_1"] a[href]')).slice(0,8).map(a=>({href:a.href, text: (a.innerText.trim().substring(0,35) || 'Partido'), isLive:false})).filter(x=>x.href.includes('/partido/')||x.href.includes('/match/'));
                        if (fallback.length) return fallback;
                        // Si aún vacío, no retornar vacío: extrae texto plano de .event__match
                        const plain = Array.from(document.querySelectorAll('.event__match')).slice(0,6).map((c,i)=>{
                            const a = c.querySelector('a');
                            return a ? {href: a.href, text: c.innerText.trim().substring(0,35), isLive:true} : null;
                        }).filter(Boolean);
                        return plain;
                    }
                    return result;
                }""")
            except Exception as e:
                partidos = []
            await browser.close()
    except Exception as e:
        logging.exception(f"Error Playwright /hoy: {e}")
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
    status_msg = await message.answer(f"🔍 Procesando combinada de {len(partidos)} partidos... extrayendo datos (20-40s)")
    textos_combinados = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 800}, locale="es-CO", extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"}, java_script_enabled=True)
            for idx, url in enumerate(partidos, 1):
                try:
                    page = await context.new_page()
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                    await page.goto(url, timeout=60000, wait_until="commit")
                    await asyncio.sleep(random.uniform(1.5, 3.0))
                    try:
                        await page.wait_for_selector("body", timeout=8000)
                    except Exception:
                        pass
                    body_text = await page.inner_text("body")
                    textos_combinados.append(f"--- PARTIDO {idx}: {url} ---\n{body_text[:2500]}")
                    await page.close()
                except Exception as e:
                    logging.exception(f"Error scraping combinada {idx}: {e}")
                    textos_combinados.append(f"--- PARTIDO {idx}: {url} ---\n[Error extrayendo datos]")
            await browser.close()
    except Exception as e:
        logging.exception(f"Error Playwright combinada: {e}")
        await status_msg.edit_text("❌ Error extrayendo datos de la combinada. Intenta de nuevo, mi hermano.")
        return
    if not textos_combinados:
        await status_msg.edit_text("❌ No se pudo extraer datos.")
        return
    await status_msg.edit_text("📊 Datos leídos. Calculando viabilidad conjunta del parlay...")
    try:
        model = genai.GenerativeModel(model_name="gemini-3.6-flash", system_instruction=SYSTEM_INSTRUCTION_COMBINADA)
        prompt = f"Datos combinados para Combinada/Parlay de {len(partidos)} partidos:\n\n" + "\n\n".join(textos_combinados)
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
        logging.exception(f"Error Gemini combinada: {e}")
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
        "🎯 *Modo Combinada activado!*\nEnvíame enlaces de Flashscore uno por uno.\nCuando termines pulsa 🔥 Calcular Parlay.",
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
        if "flashscore" in text_lower:
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
            f"Envíame enlaces de Flashscore.",
            reply_markup=kb_combinada()
        )
        return

    # === PRIORIDAD 1: Análisis Individual (fuera de combinada) ===
    # Si es enlace Flashscore y NO está en modo combinada, ejecutar análisis individual de inmediato
    if "flashscore" in text_lower and current_state != AnalysisStates.collecting_combinada.state:
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
    if "flashscore" not in url.lower():
        if url.startswith("http"):
            await message.answer("⚠️ Por favor envía un enlace válido de Flashscore (debe contener 'flashscore').\nSi quieres evaluar una cuota escríbela así: `1.90 al ambos anotan`\n🎯 Para parlays usa /combinada", reply_markup=kb_proactivo())
        return
    status_msg = await message.answer("🔍 Enlace válido, parcero. Iniciando extracción con Playwright (headless)... pilas pues, esto toma unos segundos.")
    scraped_text = ""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36", viewport={"width": 1280, "height": 800}, locale="es-CO", extra_http_headers={"Accept-Language": "es-ES,es;q=0.9"}, java_script_enabled=True)
            page = await context.new_page()
            await asyncio.sleep(random.uniform(1.0, 3.0))
            await page.goto(url, timeout=60000, wait_until="commit")
            await asyncio.sleep(random.uniform(2.0, 4.5))
            try:
                await page.wait_for_selector("body", timeout=10000)
            except Exception:
                pass
            body_text = await page.inner_text("body")
            scraped_text = body_text[:3000]
            await browser.close()
    except Exception as e:
        logging.exception(f"Error Playwright: {e}")
        await status_msg.edit_text("❌ Error al extraer datos de Flashscore. Verifica el enlace o intenta más tarde, mi hermano.")
        return
    if not scraped_text.strip():
        await status_msg.edit_text("❌ No se pudo extraer contenido. El sitio puede estar bloqueando el scraping.")
        return
    await state.update_data(scraped_text=scraped_text, last_url=url)
    await status_msg.edit_text("📊 Estadísticas leídas. Analizando con lupa alineaciones y bajas para sacarte las mejores opciones...")
    try:
        model = genai.GenerativeModel(model_name="gemini-3.6-flash", system_instruction=SYSTEM_INSTRUCTION)
        prompt = f"Datos extraídos de Flashscore (H2H, alineaciones probables, bajas de jugadores clave, árbitro, tendencias) - Partido: {url}\n\n{scraped_text}"
        response = await asyncio.to_thread(model.generate_content, prompt)
        analysis_result = response.text.strip() if hasattr(response, "text") and response.text else str(response)
        increment_user_usage(telegram_id)
        await message.answer(analysis_result, reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_quota_or_chat)
    except Exception as e:
        logging.exception(f"Error Gemini proactivo: {e}")
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
        await message.answer("⚠️ No tengo contexto del partido, mi hermano. Envíame primero un enlace de Flashscore y luego evaluamos la cuota que quieras.", reply_markup=kb_proactivo())
        await state.set_state(AnalysisStates.waiting_for_link)
        return
    if len(quota_input) < 3:
        await message.answer("Pilas pues, dime la cuota y mercado: ej. `1.90 al ambos anotan` o `2.10 en +4.5 tarjetas` y te digo si hay valor o si nos quemamos.", reply_markup=kb_proactivo())
        return
    processing_msg = await message.answer("🤖 Analizando tu elección, parcero... calculando EV y stake...")
    try:
        model = genai.GenerativeModel(model_name="gemini-3.6-flash", system_instruction=SYSTEM_INSTRUCTION_CUOTA)
        prompt = (
            f"Contexto del partido (Flashscore - {last_url}):\n{scraped_text}\n\n"
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
        logging.exception(f"Error Gemini cuota: {e}")
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
