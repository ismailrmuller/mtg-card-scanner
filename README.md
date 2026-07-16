# MTG Card Scanner

A physical + software system for cataloguing a Magic: The Gathering card collection. An ESP32-CAM shoots photos of cards on button press; a Python desktop app receives them, uses Claude AI to identify each card, stores the results in SQLite, and provides a searchable gallery with deck management.

---

## Hardware

| Part | Notes |
|---|---|
| ESP32-CAM AI-Thinker | Main microcontroller + OV2640 camera |
| Tactile push-button | Wired between GPIO13 and GND |
| Active buzzer (optional) | Wired to GPIO12; set `BUZZER_PIN -1` to disable |
| FTDI USB-serial adapter | For flashing only (the ESP32-CAM has no USB) |

---

## Software prerequisites

- **Python 3.10+**
- **PlatformIO** (CLI or VS Code extension) — for flashing the firmware
- **Anthropic API key** — card identification uses `claude-sonnet-4-5` vision

---

## Setup

### 1. Clone

```bash
git clone https://github.com/YOUR_USERNAME/card_scanner.git
cd card_scanner
```

### 2. Firmware credentials

```bash
cp include/config.example.h include/config.h
```

Edit `include/config.h`:
- Set `WIFI_SSID` and `WIFI_PASSWORD` to your network
- Set `SERVER_IP` to the LAN IP of the PC that will run the receiver (`ipconfig` on Windows, `ip a` on Linux)

### 3. Flash the ESP32-CAM

```bash
pio run --target upload
```

Connect via FTDI, hold the IO0 button while powering up to enter flash mode.

### 4. Python dependencies

```bash
cd server
pip install -r requirements.txt
```

### 5. API key

```bash
cp server/.env.example server/.env
```

Edit `server/.env` and paste your Anthropic API key.

---

## Running

**Windows** — double-click `MTG Scanner.bat`

**Any OS:**
```bash
cd server
python app.py
```

The app opens a desktop window with six tabs:

| Tab | Purpose |
|---|---|
| Galeria | Searchable card grid; click any card for full details + lightbox |
| Escanear | Start/stop the WiFi receiver; live feed of incoming scans |
| Processar | Run AI identification on unprocessed scans |
| Exportar | Export collection to HTML/Excel or AI-readable CSV |
| Config | Store your Anthropic API key (saved to `.env`) |
| Decks | Import AI-generated deck `.md` files; create and manage decks |

---

## Deck file format

Decks can be described by an AI (e.g. Claude) in a structured `.md` file and imported directly. See [`docs/deck_format.md`](docs/deck_format.md) for the spec. *(Example: ask Claude "Build me a casual green/white token deck in this format.")*

---

## Project structure

```
card_scanner/
├── include/
│   ├── config.example.h   # copy to config.h and fill in credentials
│   └── config.h           # gitignored
├── src/
│   └── main.cpp           # ESP32-CAM firmware (Arduino/PlatformIO)
├── server/
│   ├── app.py             # Flet desktop GUI (6 tabs)
│   ├── receiver.py        # HTTP server that receives JPEGs from the camera
│   ├── identify_cards.py  # Claude vision → SQLite pipeline
│   ├── import_deck.py     # Deck .md parser + DB importer
│   ├── export_ai.py       # CSV export for AI analysis
│   ├── .env.example       # copy to .env and add API key
│   └── requirements.txt
├── platformio.ini
├── MTG Scanner.bat        # Windows launcher
└── LICENSE
```

---

## License

MIT — see [LICENSE](LICENSE).
