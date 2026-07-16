#pragma once
// Copy this file to config.h and fill in your values.
// config.h is gitignored — never commit your credentials.

// ── WiFi ──────────────────────────────────────────────────────────────────
#define WIFI_SSID           "YOUR_WIFI_SSID"
#define WIFI_PASSWORD       "YOUR_WIFI_PASSWORD"

// ── Server ────────────────────────────────────────────────────────────────
// LAN IP of the PC running receiver.py (find with `ipconfig` on Windows)
#define SERVER_IP           "192.168.1.XXX"
#define SERVER_PORT         8765

// ── Input ─────────────────────────────────────────────────────────────────
// Button wired between GPIO13 and GND (INPUT_PULLUP, active LOW)
#define BUTTON_PIN          13

// ── Capture ───────────────────────────────────────────────────────────────
// JPEG quality sent to server: 0–63, lower = better quality / larger file
#define JPEG_QUALITY        10

// OV2640 warm-up after a mode switch (ms)
#define CAMERA_WARMUP_MS    300

// ── Transmission ──────────────────────────────────────────────────────────
#define MAX_RETRIES         3
#define RETRY_DELAY_MS      500

// ── Timeouts ──────────────────────────────────────────────────────────────
#define WIFI_TIMEOUT_MS     20000
#define HTTP_TIMEOUT_MS     10000

// ── GPIO ──────────────────────────────────────────────────────────────────
// Onboard flash LED — keep LOW during capture, double-flash on send success
#define FLASH_LED_PIN       4

// Active buzzer — set to -1 to disable entirely
#define BUZZER_PIN          12
