// MTG Card Scanner — ESP32-CAM AI-Thinker
// Button-press mode: place card → press button → photo taken → uploaded → repeat

#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include "esp_camera.h"
#include <time.h>
#include "config.h"

// ── AI-Thinker ESP32-CAM pin map ──────────────────────────────────────────
#define PWDN_GPIO_NUM   32
#define RESET_GPIO_NUM  -1
#define XCLK_GPIO_NUM    0
#define SIOD_GPIO_NUM   26
#define SIOC_GPIO_NUM   27
#define Y9_GPIO_NUM     35
#define Y8_GPIO_NUM     34
#define Y7_GPIO_NUM     39
#define Y6_GPIO_NUM     36
#define Y5_GPIO_NUM     21
#define Y4_GPIO_NUM     19
#define Y3_GPIO_NUM     18
#define Y2_GPIO_NUM      5
#define VSYNC_GPIO_NUM  25
#define HREF_GPIO_NUM   23
#define PCLK_GPIO_NUM   22

#define JPEG_BUF_CAP  (400 * 1024)

static uint8_t* g_jpeg_buf   = nullptr;
static size_t   g_jpeg_len   = 0;
static int      g_scan_count = 0;

// ── GPIO helpers ───────────────────────────────────────────────────────────
static void led_flash(int on_ms) {
    digitalWrite(FLASH_LED_PIN, HIGH);
    delay(on_ms);
    digitalWrite(FLASH_LED_PIN, LOW);
}

static void beep(int on_ms) {
    if (BUZZER_PIN < 0) return;
    digitalWrite(BUZZER_PIN, HIGH);
    delay(on_ms);
    digitalWrite(BUZZER_PIN, LOW);
}

// ── Camera ─────────────────────────────────────────────────────────────────
static bool camera_init() {
    // Power-cycle the OV2640 — PWDN is active HIGH on AI-Thinker
    pinMode(PWDN_GPIO_NUM, OUTPUT);
    digitalWrite(PWDN_GPIO_NUM, HIGH);
    delay(100);
    digitalWrite(PWDN_GPIO_NUM, LOW);
    delay(100);

    camera_config_t c = {};
    c.pin_pwdn      = PWDN_GPIO_NUM;
    c.pin_reset     = RESET_GPIO_NUM;
    c.pin_xclk      = XCLK_GPIO_NUM;
    c.pin_sscb_sda  = SIOD_GPIO_NUM;
    c.pin_sscb_scl  = SIOC_GPIO_NUM;
    c.pin_d7 = Y9_GPIO_NUM;  c.pin_d6 = Y8_GPIO_NUM;
    c.pin_d5 = Y7_GPIO_NUM;  c.pin_d4 = Y6_GPIO_NUM;
    c.pin_d3 = Y5_GPIO_NUM;  c.pin_d2 = Y4_GPIO_NUM;
    c.pin_d1 = Y3_GPIO_NUM;  c.pin_d0 = Y2_GPIO_NUM;
    c.pin_vsync     = VSYNC_GPIO_NUM;
    c.pin_href      = HREF_GPIO_NUM;
    c.pin_pclk      = PCLK_GPIO_NUM;
    c.xclk_freq_hz  = 20000000;
    c.ledc_timer    = LEDC_TIMER_0;
    c.ledc_channel  = LEDC_CHANNEL_0;
    c.pixel_format  = PIXFORMAT_JPEG;
    c.frame_size    = FRAMESIZE_UXGA;
    c.jpeg_quality  = JPEG_QUALITY;
    c.fb_count      = 1;
    c.fb_location   = CAMERA_FB_IN_PSRAM;
    c.grab_mode     = CAMERA_GRAB_WHEN_EMPTY;

    if (esp_camera_init(&c) != ESP_OK) {
        Serial.println("[CAM] init failed");
        return false;
    }

    sensor_t* s = esp_camera_sensor_get();
    if (s) {
        s->set_whitebal(s, 1);
        s->set_awb_gain(s, 1);
        s->set_wb_mode(s, 0);
        s->set_exposure_ctrl(s, 1);
        s->set_gain_ctrl(s, 1);
        s->set_special_effect(s, 0);
        s->set_saturation(s, 0);
    }

    // Let AWB and AEC converge before the first scan
    Serial.print("[CAM] warming up");
    delay(800);
    for (int i = 0; i < 8; i++) {
        camera_fb_t* f = esp_camera_fb_get();
        if (f) esp_camera_fb_return(f);
        Serial.print(".");
    }
    Serial.println(" ready");
    return true;
}

// ── WiFi ───────────────────────────────────────────────────────────────────
static void wifi_connect() {
    Serial.printf("[WiFi] connecting to %s", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    unsigned long t0 = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - t0 > WIFI_TIMEOUT_MS) {
            Serial.println("\n[WiFi] timeout — restarting");
            ESP.restart();
        }
        delay(500);
        Serial.print(".");
    }
    Serial.printf("\n[WiFi] %s  RSSI %d dBm\n",
        WiFi.localIP().toString().c_str(), WiFi.RSSI());
}

static void ntp_sync() {
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    Serial.print("[NTP] syncing");
    struct tm t;
    int tries = 0;
    while (!getLocalTime(&t, 1000) && tries++ < 15) Serial.print(".");
    Serial.println(tries < 15 ? " ok" : " failed (millis fallback)");
}

static void make_filename(char* buf, size_t cap) {
    struct tm t;
    if (getLocalTime(&t, 0)) {
        snprintf(buf, cap, "card_%04d%02d%02d_%02d%02d%02d_%03d.jpg",
            t.tm_year + 1900, t.tm_mon + 1, t.tm_mday,
            t.tm_hour, t.tm_min, t.tm_sec,
            g_scan_count % 1000);
    } else {
        snprintf(buf, cap, "card_%09lu_%03d.jpg", millis() / 1000, g_scan_count % 1000);
    }
}

// ── HTTP upload ────────────────────────────────────────────────────────────
static bool http_post_jpeg(const uint8_t* buf, size_t len, const char* filename) {
    char url[64];
    snprintf(url, sizeof(url), "http://%s:%d/upload", SERVER_IP, SERVER_PORT);

    const char* boundary = "ESP32CAMbndry";
    char hdr[256];
    int hlen = snprintf(hdr, sizeof(hdr),
        "--%s\r\nContent-Disposition: form-data; name=\"card\"; filename=\"%s\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n", boundary, filename);
    char ftr[48];
    int flen = snprintf(ftr, sizeof(ftr), "\r\n--%s--\r\n", boundary);

    size_t total = hlen + len + flen;
    uint8_t* body = (uint8_t*)ps_malloc(total);
    if (!body) body = (uint8_t*)malloc(total);
    if (!body) { Serial.printf("[HTTP] alloc %u B failed\n", total); return false; }

    memcpy(body,              hdr, hlen);
    memcpy(body + hlen,       buf, len);
    memcpy(body + hlen + len, ftr, flen);

    char ct[64];
    snprintf(ct, sizeof(ct), "multipart/form-data; boundary=%s", boundary);

    HTTPClient http;
    http.begin(url);
    http.setTimeout(HTTP_TIMEOUT_MS);
    http.addHeader("Content-Type", ct);
    int code = http.POST(body, total);
    http.end();
    free(body);

    Serial.printf("[HTTP] %s -> %d  (%u B)\n", filename, code, len);
    return code == 200;
}

// ── Capture ────────────────────────────────────────────────────────────────
static bool do_capture() {
    // Flush one stale frame, then grab a fresh one
    camera_fb_t* fb = esp_camera_fb_get();
    if (fb) esp_camera_fb_return(fb);

    fb = esp_camera_fb_get();
    if (!fb || fb->format != PIXFORMAT_JPEG || fb->len == 0) {
        Serial.println("[CAPTURE] no valid frame");
        if (fb) esp_camera_fb_return(fb);
        return false;
    }
    if (fb->len > JPEG_BUF_CAP) {
        Serial.printf("[CAPTURE] frame too large (%u B) — discarding\n", fb->len);
        esp_camera_fb_return(fb);
        return false;
    }
    memcpy(g_jpeg_buf, fb->buf, fb->len);
    g_jpeg_len = fb->len;
    esp_camera_fb_return(fb);
    Serial.printf("[CAPTURE] %u B\n", g_jpeg_len);
    return true;
}

// ── Setup ──────────────────────────────────────────────────────────────────
void setup() {
    delay(500);
    Serial.begin(115200);
    Serial.println("\n[BOOT] MTG Card Scanner — button mode");

    pinMode(FLASH_LED_PIN, OUTPUT);
    digitalWrite(FLASH_LED_PIN, LOW);
    if (BUZZER_PIN >= 0) { pinMode(BUZZER_PIN, OUTPUT); digitalWrite(BUZZER_PIN, LOW); }
    pinMode(BUTTON_PIN, INPUT_PULLUP);

    g_jpeg_buf = (uint8_t*)ps_malloc(JPEG_BUF_CAP);
    if (!g_jpeg_buf) {
        Serial.println("[BOOT] PSRAM alloc FAILED — halting");
        while (true) delay(1000);
    }

    wifi_connect();
    ntp_sync();

    if (!camera_init()) {
        Serial.println("[BOOT] camera FAILED — halting");
        while (true) delay(1000);
    }

    beep(200);
    Serial.println("[READY] place card and press button");
}

// ── Loop ───────────────────────────────────────────────────────────────────
void loop() {
    if (digitalRead(BUTTON_PIN) != LOW) return;
    delay(50);                                   // debounce
    if (digitalRead(BUTTON_PIN) != LOW) return;

    Serial.println("[SCAN] capturing...");

    if (!do_capture()) {
        beep(50); delay(50); beep(50); delay(50); beep(50);
        while (digitalRead(BUTTON_PIN) == LOW) delay(10);
        return;
    }

    g_scan_count++;
    beep(100);

    char filename[48];
    make_filename(filename, sizeof(filename));

    bool ok = false;
    for (int attempt = 0; attempt <= MAX_RETRIES; attempt++) {
        if (attempt > 0) { delay(RETRY_DELAY_MS); Serial.printf("[SEND] retry %d\n", attempt); }
        if (http_post_jpeg(g_jpeg_buf, g_jpeg_len, filename)) { ok = true; break; }
    }
    g_jpeg_len = 0;

    if (ok) {
        led_flash(80); delay(80); led_flash(80);
        beep(200);
        Serial.printf("[DONE] scan #%d — %s\n", g_scan_count, filename);
    } else {
        beep(50); delay(50); beep(50); delay(50); beep(50);
        Serial.println("[DONE] upload failed");
    }

    while (digitalRead(BUTTON_PIN) == LOW) delay(10);  // wait for release
    delay(300);
    Serial.println("[READY] place card and press button");
}
