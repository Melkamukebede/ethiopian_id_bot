"""
Ethiopian Digital ID Card Generator — Telegram Bot
====================================================
Send any Fayda PDF → receive front + back ID card images.

Setup:
  pip install python-telegram-bot pdfplumber pymupdf Pillow
  export BOT_TOKEN="your_token_here"
  python bot.py
"""

import os
import re
import io
import logging
import tempfile
from pathlib import Path

import pdfplumber
import pymupdf                          # fitz replacement
from PIL import Image, ImageDraw, ImageFont
from telegram import Update, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder, CommandHandler,
    MessageHandler, filters, ContextTypes
)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
# PASTE YOUR TOKEN FROM BOTFATHER DIRECTLY HERE (between the quotes):
BOT_TOKEN = "7958183039:AAFWSsZE73QjyT62PX2-b3uOtLRcCsnxTlA"

# Template paths — put your PNGs next to bot.py
BASE_DIR       = Path(__file__).parent
TEMPLATE_FRONT = BASE_DIR / "template_front.png"
TEMPLATE_BACK  = BASE_DIR / "template_back.png"

# Fonts (installed via: apt install fonts-noto-core fonts-noto-extra)
_NOTO = Path("/usr/share/fonts/truetype/noto")
FONT_ETH_REG  = str(_NOTO / "NotoSansEthiopic-Regular.ttf")
FONT_ETH_BOLD = str(_NOTO / "NotoSansEthiopic-Bold.ttf")
FONT_LAT_REG  = str(_NOTO / "NotoSans-Regular.ttf")
FONT_LAT_BOLD = str(_NOTO / "NotoSans-Bold.ttf")

# Card colours
C_DARK = (30, 30, 30, 255)      # near-black for data text
C_GOLD = (120, 85, 20, 255)     # label colour (not used for data)


# ── Font helpers ──────────────────────────────────────────────────────────────
def font(size: int, bold: bool = False, ethiopic: bool = False) -> ImageFont.FreeTypeFont:
    path = (FONT_ETH_BOLD if bold else FONT_ETH_REG) if ethiopic else \
           (FONT_LAT_BOLD if bold else FONT_LAT_REG)
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        logger.warning("Font not found: %s, falling back to default", path)
        return ImageFont.load_default()


# ── PDF Extraction ────────────────────────────────────────────────────────────

# ── Known vocabulary for 2-column splitting ───────────────────────────────────
_SEX_AM  = {"ሴት", "ወንድ"}
_SEX_EN  = {"Female", "Male"}
_NAT_AM  = {"ኢትዮጵያ"}
_NAT_EN  = {"Ethiopian"}


def _split_known(line: str, first_vocab: set) -> tuple[str, str]:
    """
    Split a 2-column PDF line where col1 is a known word from first_vocab.
    Falls back to first whitespace token if no known word matches.
    """
    line = line.strip()
    for word in sorted(first_vocab, key=len, reverse=True):
        if line.startswith(word):
            return word, line[len(word):].strip()
    parts = line.split(" ", 1)
    return parts[0], parts[1].strip() if len(parts) > 1 else ""


def extract_data(pdf_path: str) -> dict:
    """
    Parse a Fayda (Ethiopian Digital ID) PDF and return a dict of all fields.
    The PDF stores data in a 2-column layout after an FCN anchor line.
    Returns empty strings for any field that could not be found.
    """
    with pdfplumber.open(pdf_path) as pdf:
        raw = "\n".join(page.extract_text() or "" for page in pdf.pages)

    lines = [ln for ln in raw.split("\n") if ln.strip()]

    # ── Locate the FCN line (e.g. "3675 9124 0985 4263 Kedija Roba Geda") ────
    ds = next(
        (i for i, ln in enumerate(lines)
         if re.search(r"\d{4}\s+\d{4}\s+\d{4}\s+\d{4}", ln)),
        -1,
    )
    if ds < 0:
        logger.warning("FCN line not found in PDF — extraction may be incomplete")
        return {k: "" for k in [
            "name_am","name_en","fcn","dob_et","dob_en","sex_am","sex_en",
            "nat_am","nat_en","phone","region_am","region_en",
            "zone_am","zone_en","woreda_am","woreda_en",
            "date_issue","date_exp_et","date_exp_en",
        ]}

    # ── Amharic name is the line directly before FCN ──────────────────────────
    name_am = lines[ds - 1].strip() if ds > 0 else ""

    # ── FCN line also contains the English name ───────────────────────────────
    fcn_m   = re.match(r"([\d\s]{15,})\s+([A-Za-z].+)", lines[ds])
    fcn     = fcn_m.group(1).strip() if fcn_m else ""
    name_en = re.sub(r"\(cid:\d+\)", "i", fcn_m.group(2).strip()) if fcn_m else ""

    def L(offset: int) -> str:
        idx = ds + offset
        return lines[idx].strip() if idx < len(lines) else ""

    # ── Line ds+1: "29/11/1948 ኦሮሚያ" ──────────────────────────────────────
    m1 = re.match(r"(\d{2}/\d{2}/\d{4})\s+(.+)", L(1))
    dob_et    = m1.group(1) if m1 else ""
    region_am = m1.group(2).strip() if m1 else ""

    # ── Line ds+2: "1956/08/05 Oromia" ─────────────────────────────────────
    m2 = re.match(r"(\d{4}/\d{2}/\d{2})\s+(.+)", L(2))
    dob_en    = m2.group(1) if m2 else ""
    region_en = m2.group(2).strip() if m2 else ""

    # ── Line ds+3: "ሴት ባሌ"  →  sex_am, zone_am ─────────────────────────────
    sex_am, zone_am = _split_known(L(3), _SEX_AM)

    # ── Line ds+4: "Female Bale"  →  sex_en, zone_en ────────────────────────
    sex_en, zone_en = _split_known(L(4), _SEX_EN)

    # ── Line ds+5: "ኢትዮጵያ ዶሎ ማና"  →  nat_am, woreda_am ──────────────────
    nat_am, woreda_am = _split_known(L(5), _NAT_AM)

    # ── Line ds+6: "Ethiopian Delo Mena"  →  nat_en, woreda_en ─────────────
    nat_en, woreda_en = _split_known(L(6), _NAT_EN)

    # ── Line ds+7: phone number ──────────────────────────────────────────────
    ph_m  = re.search(r"0\d{9}", L(7))
    phone = ph_m.group(0) if ph_m else L(7)

    return {
        "name_am":     name_am,
        "name_en":     name_en,
        "fcn":         fcn,
        "dob_et":      dob_et,
        "dob_en":      dob_en,
        "sex_am":      sex_am,
        "sex_en":      sex_en,
        "nat_am":      nat_am  or "ኢትዮጵያ",
        "nat_en":      nat_en  or "Ethiopian",
        "phone":       phone,
        "region_am":   region_am,
        "region_en":   region_en,
        "zone_am":     zone_am,
        "zone_en":     zone_en,
        "woreda_am":   woreda_am,
        "woreda_en":   woreda_en,
        "date_issue":  "",
        "date_exp_et": "",
        "date_exp_en": "",
    }


def extract_photo(pdf_path: str) -> Image.Image | None:
    """Return the person's portrait from the PDF (largest face-sized JPEG)."""
    doc = pymupdf.open(pdf_path)
    candidates = []
    for page in doc:
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            raw_img = doc.extract_image(xref)
            w, h = raw_img["width"], raw_img["height"]
            # Portrait = taller than wide, reasonable size
            if h > w and 200 < w < 800 and 200 < h < 1000:
                candidates.append((w * h, raw_img["image"], raw_img["ext"]))
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0])      # smallest qualifying = ID photo
    _, img_bytes, _ = candidates[0]
    return Image.open(io.BytesIO(img_bytes)).convert("RGBA")


# ── Card Rendering ────────────────────────────────────────────────────────────
def draw_text(draw: ImageDraw.Draw, xy: tuple, text: str,
              size: int, bold: bool = False,
              ethiopic: bool = False, colour=C_DARK):
    """Draw text at pixel position xy."""
    f = font(size, bold=bold, ethiopic=ethiopic)
    draw.text(xy, text, font=f, fill=colour)


def render_front(data: dict, photo: Image.Image | None) -> Image.Image:
    """Compose the front face of the ID card."""
    card = Image.open(TEMPLATE_FRONT).convert("RGBA")
    draw = ImageDraw.Draw(card)
    W, H = card.size        # 1045 × 651

    # ── Photo ─────────────────────────────────────────────────────────────────
    if photo:
        ph = photo.resize((175, 222), Image.LANCZOS)
        card.paste(ph, (32, 120), ph)

    # ── Date of Issue (left-side vertical strip) ──────────────────────────────
    if data["date_issue"]:
        draw_text(draw, (8, 228), data["date_issue"], size=13, bold=False)

    # ── Full Name ─────────────────────────────────────────────────────────────
    draw_text(draw, (240, 163), data["name_am"], size=22,
              bold=True, ethiopic=True)
    draw_text(draw, (240, 193), data["name_en"], size=17, bold=False)

    # ── Date of Birth ─────────────────────────────────────────────────────────
    dob_str = f"{data['dob_et']}  |  {data['dob_en']}" \
        if data["dob_et"] and data["dob_en"] else (data["dob_et"] or data["dob_en"])
    draw_text(draw, (240, 292), dob_str, size=16, bold=True)

    # ── Sex ───────────────────────────────────────────────────────────────────
    sex_str = f"{data['sex_am']}  |  {data['sex_en']}" \
        if data["sex_am"] and data["sex_en"] else (data["sex_am"] or data["sex_en"])
    draw_text(draw, (240, 344), sex_str, size=16, bold=True)

    # ── Date of Expiry ────────────────────────────────────────────────────────
    exp_str = f"{data['date_exp_et']}  |  {data['date_exp_en']}" \
        if data["date_exp_et"] and data["date_exp_en"] else ""
    if exp_str:
        draw_text(draw, (240, 420), exp_str, size=16, bold=True)

    # ── Card Number (FCN) ─────────────────────────────────────────────────────
    if data["fcn"]:
        draw_text(draw, (490, 507), data["fcn"], size=15, bold=True)

    return card.convert("RGB")


def render_back(data: dict, photo: Image.Image | None) -> Image.Image:
    """Compose the back face of the ID card."""
    card = Image.open(TEMPLATE_BACK).convert("RGBA")
    draw = ImageDraw.Draw(card)

    # ── Phone ─────────────────────────────────────────────────────────────────
    if data["phone"]:
        draw_text(draw, (30, 72), data["phone"], size=18, bold=True)

    # ── Nationality ───────────────────────────────────────────────────────────
    nat_str = f"{data['nat_am']} | {data['nat_en']}"
    draw_text(draw, (30, 195), nat_str, size=18, bold=True, ethiopic=True)

    # ── Address ───────────────────────────────────────────────────────────────
    y = 268
    for am, en in [
        (data["region_am"],  data["region_en"]),
        (data["zone_am"],    data["zone_en"]),
        (data["woreda_am"],  data["woreda_en"]),
    ]:
        if am:
            draw_text(draw, (30, y),      am, size=16, bold=True,  ethiopic=True)
            draw_text(draw, (30, y + 24), en, size=15, bold=False)
            y += 56

    # ── Small photo (top-right box) ───────────────────────────────────────────
    if photo:
        ph = photo.resize((222, 282), Image.LANCZOS)
        card.paste(ph, (478, 28), ph)

    return card.convert("RGB")


def build_id_card(pdf_path: str) -> tuple[bytes, bytes]:
    """
    Full pipeline: PDF → extract → render.
    Returns (front_jpeg_bytes, back_jpeg_bytes).
    """
    data  = extract_data(pdf_path)
    photo = extract_photo(pdf_path)

    front_img = render_front(data, photo)
    back_img  = render_back(data, photo)

    def to_bytes(img: Image.Image) -> bytes:
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        return buf.getvalue()

    return to_bytes(front_img), to_bytes(back_img)


# ── Telegram Handlers ─────────────────────────────────────────────────────────
async def cmd_start(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "👋 *Ethiopian Digital ID Card Generator*\n\n"
        "📄 Send me a Fayda PDF (your Ethiopian Digital ID document) "
        "and I will generate a filled-in ID card image for you — "
        "both *front* and *back* sides.\n\n"
        "Just send the PDF now ↓",
        parse_mode="Markdown",
    )


async def cmd_help(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ *How to use:*\n\n"
        "1️⃣ Get your Fayda PDF from id.et or the Fayda app\n"
        "2️⃣ Send the PDF file to this bot\n"
        "3️⃣ Receive front + back ID card images instantly\n\n"
        "⚠️ Your document is processed locally and is *never stored*.",
        parse_mode="Markdown",
    )


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    doc = msg.document

    # Validate file type
    if not (doc.file_name or "").lower().endswith(".pdf") and \
            doc.mime_type != "application/pdf":
        await msg.reply_text(
            "❌ Please send a PDF file (the Fayda document from id.et)."
        )
        return

    # File size guard (15 MB)
    if doc.file_size and doc.file_size > 15 * 1024 * 1024:
        await msg.reply_text("❌ File too large (max 15 MB).")
        return

    status = await msg.reply_text("⏳ Processing your ID document…")

    try:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_path = os.path.join(tmp, "id.pdf")

            # Download PDF
            tg_file = await context.bot.get_file(doc.file_id)
            await tg_file.download_to_drive(pdf_path)

            # Generate card images
            front_bytes, back_bytes = build_id_card(pdf_path)

        # Send both images as a media group
        await context.bot.send_media_group(
            chat_id=msg.chat_id,
            media=[
                InputMediaPhoto(
                    media=io.BytesIO(front_bytes),
                    caption="🪪 *ID Card — Front*",
                    parse_mode="Markdown",
                ),
                InputMediaPhoto(
                    media=io.BytesIO(back_bytes),
                    caption="🪪 *ID Card — Back*",
                    parse_mode="Markdown",
                ),
            ],
        )
        await status.delete()

    except Exception as e:
        logger.exception("Error processing PDF from user %s", msg.from_user.id)
        await status.edit_text(
            f"❌ Failed to process the PDF.\n\n"
            f"Make sure this is a valid Fayda (Ethiopian Digital ID) document.\n"
            f"Error: `{type(e).__name__}`",
            parse_mode="Markdown",
        )


async def handle_other(update: Update, _: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📄 Please send me a PDF file.\n"
        "Type /help for instructions."
    )


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help",  cmd_help))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_pdf))
    app.add_handler(MessageHandler(filters.ALL, handle_other))

    logger.info("Bot running — waiting for PDFs…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
