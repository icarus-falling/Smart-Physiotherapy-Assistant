#include <WiFi.h>
#include <WebServer.h>
#include <ArduinoJson.h>
#include <ESPmDNS.h>
// ---------- Wi-Fi ----------
const char* WIFI_SSID     = "reya's F14";
const char* WIFI_PASSWORD = "12345678";
// ---------- Motor pins (Digital ON/OFF) ----------
const int MOTOR_LEFT_PIN  = 4;   // change to your pin
const int MOTOR_RIGHT_PIN = 26;  // change to your pin
// If your driver is active-LOW (motor turns on when pin is LOW), set these to false
const bool ACTIVE_HIGH_LEFT  = true;
const bool ACTIVE_HIGH_RIGHT = true;
unsigned long leftOffAt  = 0;
unsigned long rightOffAt = 0;
bool leftMotorOn  = false;
bool rightMotorOn = false;
WebServer server(80);

// ------------------ Motor Control ------------------
void setLeft(bool on) {
  leftMotorOn = on;
  digitalWrite(MOTOR_LEFT_PIN, (on ^ !ACTIVE_HIGH_LEFT) ? HIGH : LOW);
  if (on) {
    Serial.println("LEFT motor: ON");
  } else {
    Serial.println("LEFT motor: OFF");
  }
}

void setRight(bool on) {
  rightMotorOn = on;
  digitalWrite(MOTOR_RIGHT_PIN, (on ^ !ACTIVE_HIGH_RIGHT) ? HIGH : LOW);
  if (on) {
    Serial.println("RIGHT motor: ON");
  } else {
    Serial.println("RIGHT motor: OFF");
  }
}

void allOff() {
  setLeft(false);
  setRight(false);
  leftOffAt = rightOffAt = 0;
  Serial.println("All motors OFF");
}

// ------------------ HTTP Endpoints ------------------
void handleHealth() {
  StaticJsonDocument<200> doc;
  doc["status"] = "ok";
  doc["left_motor"] = leftMotorOn ? "ON" : "OFF";
  doc["right_motor"] = rightMotorOn ? "ON" : "OFF";
  doc["uptime_ms"] = millis();
  doc["wifi_rssi"] = WiFi.RSSI();
  doc["free_heap"] = ESP.getFreeHeap();
  
  String response;
  serializeJson(doc, response);
  server.send(200, "application/json", response);
  
  Serial.println("Health check requested");
}

void handleVibrate() {
  if (!server.hasArg("plain")) {
    Serial.println("ERROR: No JSON body received");
    server.send(400, "application/json", "{\"error\":\"missing body\"}");
    return;
  }

  String body = server.arg("plain");
  Serial.print("Received JSON: ");
  Serial.println(body);
  
  StaticJsonDocument<256> doc;
  DeserializationError err = deserializeJson(doc, body);
  
  if (err) {
    Serial.print("ERROR: JSON parse failed - ");
    Serial.println(err.c_str());
    server.send(400, "application/json", "{\"error\":\"invalid JSON\"}");
    return;
  }

  const char* action = doc["action"] | "";
  
  // ===== VIBRATION ON =====
  if (strcmp(action, "on") == 0) {
    const char* side = doc["side"] | "BOTH";   // LEFT/RIGHT/BOTH
    int duration_ms  = doc["duration_ms"] | 500; // default 500ms
    int intensity    = doc["intensity"] | 255;   // kept for API compatibility
    
    // Treat any intensity >= 128 as ON, < 128 as OFF
    bool shouldTurnOn = (intensity >= 128);
    
    // Constrain duration to reasonable limits
    duration_ms = constrain(duration_ms, 0, 10000); // max 10 seconds
    
    bool doLeft  = (strcasecmp(side, "LEFT")  == 0) || (strcasecmp(side, "BOTH") == 0);
    bool doRight = (strcasecmp(side, "RIGHT") == 0) || (strcasecmp(side, "BOTH") == 0);

    Serial.printf("VIB ON: side=%s, duration=%dms, intensity=%d, turning=%s\n", 
                  side, duration_ms, intensity, shouldTurnOn ? "ON" : "OFF");

    // Turn motors on/off
    if (doLeft)  setLeft(shouldTurnOn);
    if (doRight) setRight(shouldTurnOn);

    // Set auto-off timers
    unsigned long now = millis();
    if (duration_ms > 0 && shouldTurnOn) {
      if (doLeft)  leftOffAt  = now + (unsigned long)duration_ms;
      if (doRight) rightOffAt = now + (unsigned long)duration_ms;
    } else {
      // 0 duration = stay on until explicitly turned off
      if (doLeft)  leftOffAt  = 0;
      if (doRight) rightOffAt = 0;
    }

    // Send response
    StaticJsonDocument<128> resp;
    resp["status"] = "on";
    resp["side"] = side;
    resp["intensity_used"] = shouldTurnOn ? 255 : 0;
    resp["duration_ms"] = duration_ms;
    
    String response;
    serializeJson(resp, response);
    server.send(200, "application/json", response);
    return;
  } 
  
  // ===== VIBRATION OFF =====
  else if (strcmp(action, "off") == 0) {
    const char* side = doc["side"] | "BOTH";
    
    bool doLeft  = (strcasecmp(side, "LEFT")  == 0) || (strcasecmp(side, "BOTH") == 0);
    bool doRight = (strcasecmp(side, "RIGHT") == 0) || (strcasecmp(side, "BOTH") == 0);

    Serial.printf("VIB OFF: side=%s\n", side);

    if (doLeft)  { 
      setLeft(false);  
      leftOffAt = 0; 
    }
    if (doRight) { 
      setRight(false); 
      rightOffAt = 0; 
    }

    // Send response
    StaticJsonDocument<128> resp;
    resp["status"] = "off";
    resp["side"] = side;
    
    String response;
    serializeJson(resp, response);
    server.send(200, "application/json", response);
    return;
  }

  // Unknown action
  Serial.println("ERROR: Unknown action");
  server.send(400, "application/json", "{\"error\":\"unknown action\"}");
}

// Handle CORS preflight requests
void handleCors() {
  server.sendHeader("Access-Control-Allow-Origin", "*");
  server.sendHeader("Access-Control-Allow-Methods", "POST, GET, OPTIONS");
  server.sendHeader("Access-Control-Allow-Headers", "Content-Type");
  server.send(204);
}

// ------------------ Setup & Loop ------------------
void setup() {
  Serial.begin(115200);
  delay(500);
  Serial.println("\n\n=== ESP32 Vibration Controller (Non-PWM) ===");

  // Configure motor pins as outputs
  pinMode(MOTOR_LEFT_PIN, OUTPUT);
  pinMode(MOTOR_RIGHT_PIN, OUTPUT);
  
  allOff();
  Serial.printf("Motors initialized on pins L:%d R:%d\n", MOTOR_LEFT_PIN, MOTOR_RIGHT_PIN);
  Serial.printf("Active logic: LEFT=%s, RIGHT=%s\n", 
                ACTIVE_HIGH_LEFT ? "HIGH" : "LOW",
                ACTIVE_HIGH_RIGHT ? "HIGH" : "LOW");

  // Wi-Fi connection
  WiFi.mode(WIFI_STA);
  WiFi.setSleep(false); // Helps with latency on some access points
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
  
  Serial.printf("Connecting to '%s'", WIFI_SSID);
  
  int attempts = 0;
  while (WiFi.status() != WL_CONNECTED && attempts < 60) {
    delay(500);
    Serial.print(".");
    attempts++;
  }
  
  Serial.println();
  
  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("✓ WiFi connected!");
    Serial.print("IP Address: ");
    Serial.println(WiFi.localIP());
    Serial.print("Signal: ");
    Serial.print(WiFi.RSSI());
    Serial.println(" dBm");
    
    // Start mDNS for easy access via hostname
    if (MDNS.begin("esp32-haptic")) {
      Serial.println("mDNS: http://esp32-haptic.local");
      Serial.println("You can use this instead of IP address!");
    } else {
      Serial.println("mDNS start FAILED (not critical)");
    }
  } else {
    Serial.println("✗ WiFi connection FAILED!");
    Serial.println("Please check credentials and restart.");
  }

  // HTTP routes
  server.on("/health", HTTP_GET, handleHealth);
  server.on("/vibrate", HTTP_POST, handleVibrate);
  server.on("/vibrate", HTTP_OPTIONS, handleCors); // CORS support
  
  server.enableCORS(true);
  server.begin();
  
  Serial.println("HTTP server started on port 80");
  Serial.println("Ready for commands!");
  Serial.println("================================\n");
  
  // Print test instructions
  Serial.println("Test with curl:");
  Serial.printf("curl -X POST http://%s/vibrate -H \"Content-Type: application/json\" -d '{\"action\":\"on\",\"side\":\"LEFT\",\"duration_ms\":1000}'\n", WiFi.localIP().toString().c_str());
  Serial.println();
}

void loop() {
  // Handle HTTP requests
  server.handleClient();

  // Auto-off timer management
  unsigned long now = millis();
  
  if (leftOffAt && (now >= leftOffAt)) {
    setLeft(false);
    leftOffAt = 0;
    Serial.println("LEFT auto-off triggered");
  }
  
  if (rightOffAt && (now >= rightOffAt)) {
    setRight(false);
    rightOffAt = 0;
    Serial.println("RIGHT auto-off triggered");
  }

  // Safety watchdog: ensure motors don't run indefinitely without auto-off
  // If a motor has been on for 30 seconds without an auto-off timer, turn it off
  static unsigned long lastWatchdogCheck = 0;
  if (now - lastWatchdogCheck > 30000) {
    if (leftOffAt == 0 && leftMotorOn) {
      Serial.println("⚠ SAFETY WATCHDOG: LEFT motor running too long, forcing off");
      setLeft(false);
    }
    if (rightOffAt == 0 && rightMotorOn) {
      Serial.println("⚠ SAFETY WATCHDOG: RIGHT motor running too long, forcing off");
      setRight(false);
    }
    lastWatchdogCheck = now;
  }
  
  // Optional: Print status every 10 seconds when motors are active
  static unsigned long lastStatusPrint = 0;
  if ((leftMotorOn || rightMotorOn) && (now - lastStatusPrint > 10000)) {
    Serial.printf("Status: LEFT=%s, RIGHT=%s\n", 
                  leftMotorOn ? "ON" : "OFF", 
                  rightMotorOn ? "ON" : "OFF");
    lastStatusPrint = now;
  }
}