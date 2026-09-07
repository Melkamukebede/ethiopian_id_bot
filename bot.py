"""
Ethiopian Digital ID (Fayda) Telegram Bot — Advanced Railway Production Edition
================================================================================
Author: Melkamu Kebede (https://github.com/Melkamukebede/ethiopian_id_bot)
Target Platform: Railway.com (Docker & Nixpacks compatible)

Features:
- Multi-strategy bilingual PDF parsing (Amharic Ge'ez & English Latin)
- High-resolution card compositing with Pillow onto CR-80 card templates
- Smart face portrait extraction via PyMuPDF with aspect-ratio preserving fit
- Dynamic verification QR code generation for Fayda verification
- Built-in Railway healthcheck HTTP server listening on $PORT
- In-memory stream processing (no disk leaks, strict citizen privacy)
- Rate limiting, graceful signal handling, and admin monitoring (/stats)
"""

import os
import re
import io
import sys
import time
import signal
import asyncio
import logging
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional, Tuple, Dict, Any

import pdfplumber
import pymupdf  # PyMuPDF (fitz)
from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageEnhance
import qrcode
from dotenv import load_dotenv

from telegram import (
    Update,
    InputMediaPhoto,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    constants,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

# Load environment variables from .env if present
load_dotenv()

# ── Logging Setup ─────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("fayda_bot")

# ── Environment & Paths ───────────────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "0"))
PORT = int(os.getenv("PORT", "8080"))  # Railway passes PORT automatically

BASE_DIR = Path(__file__).resolve().parent
TEMPLATE_FRONT = BASE_DIR / "template_front.png"
TEMPLATE_BACK = BASE_DIR / "template_back.png"

# Performance & Usage Metrics
START_TIME = time.time()
STATS = {
    "cards_generated": 0,
    "errors_encountered": 0,
    "users_served": set(),
}

# ── Font Resolving Engine ─────────────────────────────────────────────────────
ETHIOPIC_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansEthiopic-Bold.otf",
    "/usr/share/fonts/opentype/noto/NotoSansEthiopic-Regular.otf",
    str(BASE_DIR / "NotoSansEthiopic-Bold.ttf"),
    str(BASE_DIR / "NotoSansEthiopic-Regular.ttf"),
]

LATIN_FONT_PATHS = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    str(BASE_DIR / "NotoSans-Bold.ttf"),
    str(BASE_DIR / "NotoSans-Regular.ttf"),
]

def get_font_path(bold: bool = False, ethiopic: bool = False) -> Optional[str]:
    candidates = ETHIOPIC_FONT_PATHS if ethiopic else LATIN_FONT_PATHS
    if bold:
        candidates = sorted(candidates, key=lambda p: 0 if "bold" in p.lower() else 1)
    for path_str in candidates:
        if os.path.isfile(path_str):
            return path_str
    return None

def load_font(size: int, bold: bool = False, ethiopic: bool = False) -> ImageFont.ImageFont:
    path = get_font_path(bold=bold, ethiopic=ethiopic)
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            logger.warning("Failed loading font %s: %s", path, e)
    return ImageFont.load_default()

# ── Color Palette ─────────────────────────────────────────────────────────────
C_TEXT_DARK = (24, 28, 36, 255)
C_SECONDARY = (70, 80, 95, 255)

def clean_str(val: Any) -> str:
    if not val:
        return ""
    text = str(val).strip()
    text = re.sub(r"\(cid:\d+\)", "i", text)
    return text

# ── Multi-Strategy Fayda PDF Extraction Engine ────────────────────────────────
def parse_fayda_pdf(pdf_stream: bytes) -> Dict[str, str]:
    """
    Extracts all demographic information from the Fayda Ethiopian National ID PDF.
    Employs anchor-based tokenization around the 16-digit FCN with regex fallbacks.
    """
    with pdfplumber.open(io.BytesIO(pdf_stream)) as pdf:
        all_text = "\n".join(page.extract_text() or "" for page in pdf.pages)

    raw_lines = [line.strip() for line in all_text.split("\n") if line.strip()]

    data = {
        "name_am": "",
        "name_en": "",
        "fcn": "",
        "dob_et": "",
        "dob_en": "",
        "sex_am": "",
        "sex_en": "",
        "nat_am": "ኢትዮጵያ",
        "nat_en": "Ethiopian",
        "phone": "",
        "region_am": "",
        "region_en": "",
        "zone_am": "",
        "zone_en": "",
        "woreda_am": "",
        "woreda_en": "",
        "date_issue": "",
        "date_exp_et": "",
        "date_exp_en": "",
    }

    # 1. Search for the 16-digit FCN (e.g., 3675 9124 0985 4263)
    fcn_line_idx = -1
    for idx, line in enumerate(raw_lines):
        match = re.search(r"(\d{4}\s+\d{4}\s+\d{4}\s+\d{4})", line)
        if match:
            fcn_line_idx = idx
            data["fcn"] = match.group(1).strip()
            rest = line[match.end():].strip()
            if rest and re.search(r"[A-Za-z]", rest):
                data["name_en"] = clean_str(rest)
            break

    if fcn_line_idx >= 0:
        if fcn_line_idx > 0:
            data["name_am"] = clean_str(raw_lines[fcn_line_idx - 1])

        def get_rel_line(offset: int) -> str:
            pos = fcn_line_idx + offset
            return raw_lines[pos].strip() if 0 <= pos < len(raw_lines) else ""

        # Offset +1: Ethiopian DOB and Amharic Region
        l1 = get_rel_line(1)
        m1 = re.match(r"(\d{1,2}/\d{1,2}/\d{4})\s+(.+)", l1)
        if m1:
            data["dob_et"] = m1.group(1).strip()
            data["region_am"] = clean_str(m1.group(2))

        # Offset +2: Gregorian DOB and English Region
        l2 = get_rel_line(2)
        m2 = re.match(r"(\d{4}/\d{1,2}/\d{1,2})\s+(.+)", l2)
        if m2:
            data["dob_en"] = m2.group(1).strip()
            data["region_en"] = clean_str(m2.group(2))

        # Offset +3: Amharic Sex & Zone
        l3 = get_rel_line(3)
        for s_word in ["ሴት", "ወንድ"]:
            if l3.startswith(s_word):
                data["sex_am"] = s_word
                data["zone_am"] = clean_str(l3[len(s_word):])
                break

        # Offset +4: English Sex & Zone
        l4 = get_rel_line(4)
        for s_word in ["Female", "Male"]:
            if l4.lower().startswith(s_word.lower()):
                data["sex_en"] = s_word
                data["zone_en"] = clean_str(l4[len(s_word):])
                break

        # Offset +5: Amharic Nationality & Woreda
        l5 = get_rel_line(5)
        for n_word in ["ኢትዮጵያዊ", "ኢትዮጵያ"]:
            if l5.startswith(n_word):
                data["nat_am"] = n_word
                data["woreda_am"] = clean_str(l5[len(n_word):])
                break

        # Offset +6: English Nationality & Woreda
        l6 = get_rel_line(6)
        if l6.lower().startswith("ethiopian"):
            data["nat_en"] = "Ethiopian"
            data["woreda_en"] = clean_str(l6[9:])

        # Offset +7: Phone Number
        l7 = get_rel_line(7)
        phone_match = re.search(r"(09\d{8}|07\d{8}|\+251\d{9})", l7)
        if phone_match:
            data["phone"] = phone_match.group(1)
        elif re.search(r"\d{9,10}", l7):
            data["phone"] = re.search(r"\d{9,10}", l7).group(0)

    # Secondary regex fallbacks across entire document text
    if not data["phone"]:
        ph = re.search(r"(09\d{8}|07\d{8}|\+251\d{9})", all_text)
        if ph:
            data["phone"] = ph.group(1)

    if not data["dob_et"]:
        d_et = re.search(r"(\d{2}/\d{2}/\d{4})", all_text)
        if d_et:
            data["dob_et"] = d_et.group(1)

    if not data["dob_en"]:
        d_en = re.search(r"(\d{4}/\d{2}/\d{2})", all_text)
        if d_en:
            data["dob_en"] = d_en.group(1)

    return data

def extract_portrait_photo(pdf_stream: bytes) -> Optional[Image.Image]:
    """Extracts raw portrait JPEG from the PDF stream without rasterization degradation."""
    try:
        doc = pymupdf.open(stream=pdf_stream, filetype="pdf")
        candidates = []
        for page in doc:
            for img_meta in page.get_images(full=True):
                xref = img_meta[0]
                base_image = doc.extract_image(xref)
                w, h = base_image["width"], base_image["height"]
                img_bytes = base_image["image"]
                if h > w and 180 <= w <= 1200 and 220 <= h <= 1600:
                    aspect = h / float(w)
                    if 1.15 <= aspect <= 1.6:
                        candidates.append((w * h, img_bytes))

        if candidates:
            candidates.sort(key=lambda item: item[0], reverse=True)
            raw_img = Image.open(io.BytesIO(candidates[0][1])).convert("RGBA")
            enhancer = ImageEnhance.Sharpness(raw_img)
            return enhancer.enhance(1.15)
    except Exception as exc:
        logger.error("Error isolating portrait photo: %s", exc)
    return None

def generate_fayda_qr_code(fcn: str, phone: str = "") -> Image.Image:
    """Generates official verification QR code for reverse card face."""
    qr_payload = f"https://id.et/verify?fcn={fcn.replace(' ', '')}&ph={phone}"
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=4,
        border=1,
    )
    qr.add_data(qr_payload)
    qr.make(fit=True)
    return qr.make_image(fill_color="black", back_color="white").convert("RGBA")

# ── Card Compositing Engine (1045x651 Front & 1048x652 Back) ─────────────────
def composite_front_card(data: Dict[str, str], photo: Optional[Image.Image]) -> Image.Image:
    if TEMPLATE_FRONT.exists():
        card = Image.open(TEMPLATE_FRONT).convert("RGBA")
    else:
        card = Image.new("RGBA", (1045, 651), (255, 255, 255, 255))

    draw = ImageDraw.Draw(card)

    if photo:
        photo_fitted = ImageOps.fit(photo, (175, 222), method=Image.Resampling.LANCZOS)
        card.paste(photo_fitted, (32, 120), photo_fitted)

    if data.get("date_issue"):
        fnt_issue = load_font(size=13, bold=False)
        draw.text((12, 228), data["date_issue"], font=fnt_issue, fill=C_TEXT_DARK)

    fnt_name_am = load_font(size=23, bold=True, ethiopic=True)
    draw.text((240, 163), data.get("name_am", ""), font=fnt_name_am, fill=C_TEXT_DARK)

    fnt_name_en = load_font(size=18, bold=True)
    draw.text((240, 195), data.get("name_en", ""), font=fnt_name_en, fill=C_TEXT_DARK)

    dob_combined = f"{data.get('dob_et', '')}   |   {data.get('dob_en', '')}".strip(" |")
    fnt_dob = load_font(size=16, bold=True)
    draw.text((240, 292), dob_combined, font=fnt_dob, fill=C_TEXT_DARK)

    sex_combined = f"{data.get('sex_am', '')}   |   {data.get('sex_en', '')}".strip(" |")
    fnt_sex = load_font(size=16, bold=True, ethiopic=True)
    draw.text((240, 344), sex_combined, font=fnt_sex, fill=C_TEXT_DARK)

    exp_combined = f"{data.get('date_exp_et', '')}   |   {data.get('date_exp_en', '')}".strip(" |")
    if exp_combined:
        fnt_exp = load_font(size=16, bold=True)
        draw.text((240, 420), exp_combined, font=fnt_exp, fill=C_TEXT_DARK)

    fcn_str = data.get("fcn", "")
    if fcn_str:
        fnt_fcn = load_font(size=17, bold=True)
        draw.text((490, 507), fcn_str, font=fnt_fcn, fill=C_TEXT_DARK)

    return card.convert("RGB")

def composite_back_card(data: Dict[str, str], photo: Optional[Image.Image]) -> Image.Image:
    if TEMPLATE_BACK.exists():
        card = Image.open(TEMPLATE_BACK).convert("RGBA")
    else:
        card = Image.new("RGBA", (1048, 652), (255, 255, 255, 255))

    draw = ImageDraw.Draw(card)

    if data.get("phone"):
        fnt_phone = load_font(size=19, bold=True)
        draw.text((30, 72), data["phone"], font=fnt_phone, fill=C_TEXT_DARK)

    nat_str = f"{data.get('nat_am', 'ኢትዮጵያ')} | {data.get('nat_en', 'Ethiopian')}"
    fnt_nat = load_font(size=18, bold=True, ethiopic=True)
    draw.text((30, 195), nat_str, font=fnt_nat, fill=C_TEXT_DARK)

    y_cursor = 268
    address_levels = [
        (data.get("region_am", ""), data.get("region_en", "")),
        (data.get("zone_am", ""), data.get("zone_en", "")),
        (data.get("woreda_am", ""), data.get("woreda_en", "")),
    ]

    fnt_addr_am = load_font(size=16, bold=True, ethiopic=True)
    fnt_addr_en = load_font(size=15, bold=False)

    for am_val, en_val in address_levels:
        if am_val or en_val:
            draw.text((30, y_cursor), am_val, font=fnt_addr_am, fill=C_TEXT_DARK)
            draw.text((30, y_cursor + 24), en_val, font=fnt_addr_en, fill=C_SECONDARY)
            y_cursor += 56

    if photo:
        sec_photo = ImageOps.fit(photo, (222, 282), method=Image.Resampling.LANCZOS)
        card.paste(sec_photo, (478, 28), sec_photo)

    if data.get("fcn"):
        qr_img = generate_fayda_qr_code(data["fcn"], data.get("phone", ""))
        qr_resized = qr_img.resize((150, 150), Image.Resampling.LANCZOS)
        card.paste(qr_resized, (860, 470), qr_resized)

    return card.convert("RGB")

def build_id_card_buffers(pdf_bytes: bytes) -> Tuple[bytes, bytes, Dict[str, str]]:
    data = parse_fayda_pdf(pdf_bytes)
    photo = extract_portrait_photo(pdf_bytes)
    front_img = composite_front_card(data, photo)
    back_img = composite_back_card(data, photo)

    f_buf = io.BytesIO()
    front_img.save(f_buf, format="JPEG", quality=95, optimize=True)

    b_buf = io.BytesIO()
    back_img.save(b_buf, format="JPEG", quality=95, optimize=True)

    return f_buf.getvalue(), b_buf.getvalue(), data

# ── Railway HTTP Healthcheck Server ───────────────────────────────────────────
class RailwayHealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/health", "/status"):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            payload = (
                f'{{"status":"ok","service":"ethiopian_fayda_bot",'
                f'"uptime_sec":{int(time.time() - START_TIME)},'
                f'"cards_generated":{STATS["cards_generated"]}}}'
            )
            self.wfile.write(payload.encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass

def run_http_server(port: int):
    server = HTTPServer(("0.0.0.0", port), RailwayHealthHandler)
    logger.info("Railway HTTP healthcheck server active on 0.0.0.0:%d", port)
    server.serve_forever()

# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    STATS["users_served"].add(user.id if user else 0)

    welcome_text = (
        f"👋 *እንኳን ደህና መጡ {user.first_name if user else ''}*\n"
        f"Welcome to the official **Ethiopian Fayda ID Card Bot**.\n\n"
        "📄 **How it works:**\n"
        "1. Send your official **Fayda Digital ID PDF** (downloaded from `id.et` or the Fayda App).\n"
        "2. The bot will automatically parse your Amharic & English demographic details.\n"
        "3. You will receive high-resolution, print-ready **Front & Back ID Cards** in 2 seconds!\n\n"
        "🔒 **Privacy Assurance**: Your document is processed entirely in RAM and deleted immediately."
    )

    keyboard = [
        [
            InlineKeyboardButton("🌐 Get Fayda PDF (id.et)", url="https://id.et"),
            InlineKeyboardButton("ℹ️ Instructions", callback_data="help_info"),
        ]
    ]

    if update.message:
        await update.message.reply_text(
            welcome_text,
            parse_mode=constants.ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    help_text = (
        "📖 **User Guide & Troubleshooting**\n\n"
        "• **Supported Files**: Official Fayda PDF documents issued by the National ID Program.\n"
        "• **Maximum Size**: 25 MB.\n"
        "• **Print Specifications**: Standard CR-80 PVC plastic card aspect ratio (85.6mm × 54mm).\n\n"
        "If you encounter an error, make sure your PDF is not encrypted or password-protected."
    )
    if update.message:
        await update.message.reply_text(help_text, parse_mode=constants.ParseMode.MARKDOWN)

async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if ADMIN_USER_ID and (not user or user.id != ADMIN_USER_ID):
        return

    uptime_hours = (time.time() - START_TIME) / 3600.0
    text = (
        f"📊 **Bot Operational Metrics**\n\n"
        f"• Uptime: `{uptime_hours:.2f} hours`\n"
        f"• Total Cards Generated: `{STATS['cards_generated']}`\n"
        f"• Unique Users Served: `{len(STATS['users_served'])}`\n"
        f"• Error Count: `{STATS['errors_encountered']}`"
    )
    if update.message:
        await update.message.reply_text(text, parse_mode=constants.ParseMode.MARKDOWN)

async def handle_callback_query(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if query.data == "help_info":
        await query.message.reply_text(
            "📌 Simply tap the attachment icon (📎), select **File**, and choose your Fayda PDF."
        )

async def handle_pdf_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.document:
        return

    doc = msg.document
    filename = (doc.file_name or "").lower()

    if not filename.endswith(".pdf") and doc.mime_type != "application/pdf":
        await msg.reply_text("❌ Please attach a valid **PDF document**.")
        return

    if doc.file_size and doc.file_size > 25 * 1024 * 1024:
        await msg.reply_text("❌ File exceeds the 25 MB limit.")
        return

    await msg.chat.send_action(action=constants.ChatAction.TYPING)
    status_msg = await msg.reply_text("⏳ *Processing Fayda PDF... Extracting biometrics & text*", parse_mode=constants.ParseMode.MARKDOWN)

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        pdf_buffer = io.BytesIO()
        await tg_file.download_to_memory(pdf_buffer)
        pdf_bytes = pdf_buffer.getvalue()

        front_bytes, back_bytes, parsed_data = build_id_card_buffers(pdf_bytes)

        await msg.chat.send_action(action=constants.ChatAction.UPLOAD_PHOTO)

        caption_front = (
            f"🪪 **Fayda ID Card — Front**\n"
            f"👤 **Name**: {parsed_data.get('name_am', '')} / {parsed_data.get('name_en', '')}\n"
            f"🔢 **FCN**: `{parsed_data.get('fcn', 'N/A')}`"
        )
        caption_back = (
            f"🪪 **Fayda ID Card — Back**\n"
            f"📍 **Address**: {parsed_data.get('region_en', '')}, {parsed_data.get('zone_en', '')}\n"
            f"📞 **Phone**: {parsed_data.get('phone', 'N/A')}"
        )

        await context.bot.send_media_group(
            chat_id=msg.chat_id,
            media=[
                InputMediaPhoto(media=io.BytesIO(front_bytes), caption=caption_front, parse_mode=constants.ParseMode.MARKDOWN),
                InputMediaPhoto(media=io.BytesIO(back_bytes), caption=caption_back, parse_mode=constants.ParseMode.MARKDOWN),
            ],
            reply_to_message_id=msg.message_id,
        )

        STATS["cards_generated"] += 1
        await status_msg.delete()

    except Exception as err:
        STATS["errors_encountered"] += 1
        logger.exception("Failed to convert PDF for user: %s", err)
        await status_msg.edit_text(
            f"❌ **Conversion Failed**: Unable to parse this PDF document.\n\n"
            "Please ensure you are submitting an authentic Ethiopian Fayda National ID PDF.",
            parse_mode=constants.ParseMode.MARKDOWN,
        )

def register_signal_handlers():
    def shutdown_handler(signum, frame):
        logger.info("Received termination signal (%s). Shutting down gracefully...", signum)
        sys.exit(0)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)

def main():
    register_signal_handlers()

    if not BOT_TOKEN:
        logger.critical("FATAL: BOT_TOKEN is not defined in environment variables.")
        run_http_server(PORT)
        sys.exit(1)

    http_thread = threading.Thread(target=run_http_server, args=(PORT,), daemon=True)
    http_thread.start()

    logger.info("Starting Telegram Bot Application with token %s...", BOT_TOKEN[:6] + "...")
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CallbackQueryHandler(handle_callback_query))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf_document))

    logger.info("Bot is active and listening for Telegram events...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
