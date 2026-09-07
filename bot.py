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
import pymupdf
import barcode
from barcode.writer import ImageWriter
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


def extract_images(pdf_path: str) -> tuple:
    """
    Return (portrait, qr_image) both as PIL RGBA images.
    portrait: the person's face photo (taller-than-wide JPEG)
    qr_image: the square QR code (nearly square PNG)
    Either can be None if not found.
    """
    doc = pymupdf.open(pdf_path)
    portrait = None
    qr_img   = None
    for page in doc:
        for img_info in page.get_images(full=True):
            xref    = img_info[0]
            raw_img = doc.extract_image(xref)
            w, h    = raw_img["width"], raw_img["height"]
            pil     = Image.open(io.BytesIO(raw_img["image"])).convert("RGBA")
            # Portrait: taller than wide, face-sized
            if h > w and 200 < w < 800 and 200 < h < 1200:
                if portrait is None or (w * h) < (portrait.width * portrait.height):
                    portrait = pil
            # QR code: square, medium size, PNG
            if abs(w - h) < 30 and 400 < w < 1000 and raw_img["ext"] == "png":
                qr_img = pil
    return portrait, qr_img


def extract_photo(pdf_path: str) -> Image.Image | None:
    """Compatibility wrapper — returns portrait only."""
    portrait, _ = extract_images(pdf_path)
    return portrait


# ── Card Rendering ────────────────────────────────────────────────────────────
def _make_barcode(fcn: str) -> Image.Image | None:
    """Generate a Code128 barcode from the FCN number."""
    try:
        raw = fcn.replace(" ", "")
        bc  = barcode.get("code128", raw, writer=ImageWriter())
        buf = io.BytesIO()
        bc.write(buf, options={
            "write_text": False,
            "module_height": 10,
            "quiet_zone": 1,
        })
        buf.seek(0)
        return Image.open(buf).convert("RGBA")
    except Exception as e:
        logger.warning("Barcode generation failed: %s", e)
        return None


def _put(card: Image.Image, img: Image.Image, xy: tuple, size: tuple) -> None:
    """Resize img to size and paste onto card at xy (handles RGBA mask)."""
    resized = img.resize(size, Image.LANCZOS)
    if resized.mode == "RGBA":
        card.paste(resized, xy, resized)
    else:
        card.paste(resized, xy)


def render_front(data: dict, photo: Image.Image | None,
                 qr: Image.Image | None = None) -> Image.Image:
    """Compose the front face of the ID card — pixel-perfect match to sample."""
    card = Image.open(TEMPLATE_FRONT).convert("RGBA")
    draw = ImageDraw.Draw(card)

    f_eth = font(22, bold=True,  ethiopic=True)
    f_lat = font(17, bold=False, ethiopic=False)
    f_val = font(16, bold=True,  ethiopic=False)
    f_doi = font(12, bold=False, ethiopic=False)

    # ── Large portrait (left column) ─────────────────────────────────────────
    if photo:
        _put(card, photo, (28, 108), (202, 322))

    # ── Date of Issue (left vertical strip) ──────────────────────────────────
    if data.get("date_issue"):
        draw.text((7, 200), data["date_issue"], font=f_doi, fill=C_DARK)

    # ── Full name ─────────────────────────────────────────────────────────────
    draw.text((258, 178), data["name_am"], font=f_eth, fill=C_DARK)
    draw.text((258, 210), data["name_en"], font=f_lat, fill=C_DARK)

    # ── Date of Birth (Latin only — avoids Ethiopic width issues) ─────────────
    dob = f"{data['dob_et']}  |  {data['dob_en']}" if data.get("dob_en") else data.get("dob_et","")
    draw.text((258, 292), dob, font=f_val, fill=C_DARK)

    # ── Sex (Ethiopic am + Latin en side by side) ─────────────────────────────
    f_sex_am = font(16, bold=True, ethiopic=True)
    f_sex_en = font(16, bold=True, ethiopic=False)
    draw.text((258, 358), data["sex_am"], font=f_sex_am, fill=C_DARK)
    am_w = int(draw.textlength(data["sex_am"], font=f_sex_am))
    draw.text((258 + am_w + 12, 358), f"| {data['sex_en']}", font=f_sex_en, fill=C_DARK)

    # ── Date of Expiry ────────────────────────────────────────────────────────
    exp = f"{data['date_exp_et']}  |  {data['date_exp_en']}" if data.get("date_exp_en") else data.get("date_exp_et","")
    if exp:
        draw.text((258, 438), exp, font=f_val, fill=C_DARK)

    # ── FCN card number ───────────────────────────────────────────────────────
    if data.get("fcn"):
        draw.text((490, 502), data["fcn"], font=font(15, bold=True), fill=C_DARK)

    # ── Barcode ───────────────────────────────────────────────────────────────
    if data.get("fcn"):
        bc_img = _make_barcode(data["fcn"])
        if bc_img:
            _put(card, bc_img, (485, 538), (340, 48))

    # ── Small thumbnail (bottom-right corner) ─────────────────────────────────
    if photo:
        _put(card, photo, (850, 496), (82, 104))

    return card.convert("RGB")


def render_back(data: dict, photo: Image.Image | None,
                qr: Image.Image | None = None) -> Image.Image:
    """Compose the back face of the ID card — pixel-perfect match to sample."""
    card = Image.open(TEMPLATE_BACK).convert("RGBA")
    draw = ImageDraw.Draw(card)

    f_phone  = font(20, bold=True,  ethiopic=False)
    f_am_lg  = font(19, bold=True,  ethiopic=True)
    f_en_md  = font(17, bold=False, ethiopic=False)
    f_fin    = font(14, bold=True,  ethiopic=False)

    # ── Phone ─────────────────────────────────────────────────────────────────
    if data.get("phone"):
        draw.text((28, 36), data["phone"], font=f_phone, fill=C_DARK)

    # ── Address block (region / zone / woreda) ────────────────────────────────
    # Template already prints nationality, we just draw address below ~y=262
    y = 262
    for am, en in [
        (data.get("region_am",""), data.get("region_en","")),
        (data.get("zone_am",""),   data.get("zone_en","")),
        (data.get("woreda_am",""), data.get("woreda_en","")),
    ]:
        if am:
            draw.text((28, y),      am, font=f_am_lg, fill=C_DARK)
            draw.text((28, y + 28), en, font=f_en_md, fill=C_DARK)
            y += 62

    # ── QR code (right white box  x=462, y=4, 572×540) ───────────────────────
    if qr:
        _put(card, qr, (462, 4), (572, 540))

    # ── FIN number (bottom left) ──────────────────────────────────────────────
    if data.get("fin"):
        draw.text((28, 558), f"FIN  {data['fin']}", font=f_fin, fill=C_DARK)

    return card.convert("RGB")


def build_id_card(pdf_path: str) -> tuple[bytes, bytes]:
    """
    Full pipeline: PDF → extract → render.
    Returns (front_jpeg_bytes, back_jpeg_bytes).
    """
    data           = extract_data(pdf_path)
    portrait, qr   = extract_images(pdf_path)

    front_img = render_front(data, portrait, qr)
    back_img  = render_back(data, portrait, qr)

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
