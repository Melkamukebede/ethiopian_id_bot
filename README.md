# Ethiopian Digital ID Card Bot

Telegram bot that reads a Fayda PDF and generates a filled ID card image.

## Folder Structure

```
ethiopian_id_bot/
├── bot.py               ← main bot code
├── requirements.txt
├── README.md
├── template_front.png   ← copy your template here
└── template_back.png    ← copy your template here
```

## Step 1 — Install system dependencies

```bash
# Ubuntu / Debian
sudo apt update
sudo apt install -y python3-pip fonts-noto-core fonts-noto-extra

# Verify Amharic fonts installed
fc-list | grep -i ethiopic
```

## Step 2 — Install Python libraries

```bash
pip install -r requirements.txt
```

## Step 3 — Get a Bot Token from BotFather

1. Open Telegram → search **@BotFather**
2. Send `/newbot`
3. Choose a name (e.g. "EthioID Bot")
4. Choose a username ending in `bot` (e.g. `ethio_id_maker_bot`)
5. Copy the token you receive

## Step 4 — Place template images

Copy `template_front.png` and `template_back.png` into the `ethiopian_id_bot/` folder.

## Step 5 — Run the bot

```bash
export BOT_TOKEN="123456789:ABCDefGhIJKlmNoPQRsTUVwxyZ"
python bot.py
```

Or on Windows:
```cmd
set BOT_TOKEN=123456789:ABCDefGhIJKlmNoPQRsTUVwxyZ
python bot.py
```

## Step 6 — Test it

1. Open Telegram → find your bot
2. Send `/start`
3. Send the Fayda PDF file
4. Receive front + back ID card images!

## Keep it running 24/7 (Linux server)

```bash
# Install screen
sudo apt install screen

# Start in background
screen -S idbot
export BOT_TOKEN="your_token"
python bot.py

# Detach: Ctrl+A then D
# Reattach: screen -r idbot
```

Or use systemd / PM2 for production.

## How it works

```
PDF sent by user
      ↓
pdfplumber  →  extract all text fields (name, DOB, FCN, address…)
pymupdf     →  extract portrait photo from PDF
      ↓
Pillow      →  paste photo + draw text onto template_front.png
Pillow      →  paste photo + draw address onto template_back.png
      ↓
Telegram    →  send both images back to user
```
