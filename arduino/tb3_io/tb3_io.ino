// TurtleBot3 ROS2 <-> Arduino Uno RGB / buzzer / 16x2 I2C LCD
// Serial protocol (115200 baud, newline-terminated):
//   R,255,0,0           RGB values (0..255)
//   B,1 / B,0           start / stop repeating buzzer beeps
//   L,Hello|TurtleBot3  LCD row 1 | row 2 (16 ASCII chars per row)
//   P,1 / P,0           LCD display+backlight ON / OFF
//   H                   heartbeat
//
// Library: hd44780 by Bill Perry (Arduino IDE Library Manager).
// IMPORTANT: Check your actual LED module resistor, polarity and buzzer type.

#include <Arduino.h>
#include <Wire.h>
#include <string.h>
#include <stdio.h>
#include <hd44780.h>
#include <hd44780ioClass/hd44780_I2Cexp.h>

const uint8_t RGB_R = 5;  // PWM, not Timer2
const uint8_t RGB_G = 6;  // PWM, not Timer2
const uint8_t RGB_B = 9;  // PWM, not Timer2
const uint8_t BUZZER = 8;

// Change these after inspecting the real modules.
const bool RGB_COMMON_ANODE = false;
const bool BUZZER_IS_PASSIVE = true;  // true: tone(); false: digital ON/OFF
const bool BUZZER_ACTIVE_LOW = false; // used for logic-level active buzzer modules
// A short beep followed by silence is less disruptive than a continuous tone.
// These settings change pitch and the percentage of time the buzzer sounds,
// not its instantaneous acoustic volume.
const unsigned int BUZZER_FREQUENCY_HZ = 1000;
const unsigned long BUZZER_BEEP_ON_MS = 150UL;
const unsigned long BUZZER_BEEP_OFF_MS = 600UL;

const unsigned long LINK_TIMEOUT_MS = 4000UL;
const size_t BUFFER_SIZE = 70;
const uint8_t LCD_COLUMNS = 16;
const uint8_t LCD_ROWS = 2;

hd44780_I2Cexp lcd; // automatically finds typical PCF8574/MCP23008 backpack
bool lcdReady = false;
bool lcdPowered = true;
bool linkActive = false;
bool buzzerOn = false;       // commanded state (B,1 / B,0)
bool buzzerSounding = false; // physical output state
unsigned long buzzerPhaseStartedMs = 0;
unsigned long lastMessageMs = 0;
char serialBuffer[BUFFER_SIZE];
size_t serialLength = 0;
bool droppingLongLine = false;
char lcdRows[LCD_ROWS][LCD_COLUMNS + 1] = {
  "WAIT FOR ROS2   ",
  "                "
};

void setRgb(uint8_t red, uint8_t green, uint8_t blue) {
  analogWrite(RGB_R, RGB_COMMON_ANODE ? 255 - red : red);
  analogWrite(RGB_G, RGB_COMMON_ANODE ? 255 - green : green);
  analogWrite(RGB_B, RGB_COMMON_ANODE ? 255 - blue : blue);
}

// Low-level buzzer output: used by the non-blocking repeating-beep state machine.
void driveBuzzer(bool sounding) {
  buzzerSounding = sounding;
  if (BUZZER_IS_PASSIVE) {
    if (sounding) {
      tone(BUZZER, BUZZER_FREQUENCY_HZ);
    } else {
      noTone(BUZZER);
      digitalWrite(BUZZER, BUZZER_ACTIVE_LOW ? HIGH : LOW);
    }
  } else {
    digitalWrite(BUZZER, sounding ? (BUZZER_ACTIVE_LOW ? LOW : HIGH)
                                  : (BUZZER_ACTIVE_LOW ? HIGH : LOW));
  }
}

void setBuzzer(bool on) {
  if (!on) {
    // B,0 stops immediately, even in the middle of a beep.
    buzzerOn = false;
    driveBuzzer(false);
    return;
  }
  // Repeated B,1 commands must not restart the beep cycle.
  if (buzzerOn) return;
  buzzerOn = true;
  buzzerPhaseStartedMs = millis();
  driveBuzzer(true);
}

void updateBuzzer() {
  if (!buzzerOn) return;
  const unsigned long now = millis();
  const unsigned long elapsed = now - buzzerPhaseStartedMs;
  if (buzzerSounding && elapsed >= BUZZER_BEEP_ON_MS) {
    driveBuzzer(false);
    buzzerPhaseStartedMs = now;
  } else if (!buzzerSounding && elapsed >= BUZZER_BEEP_OFF_MS) {
    driveBuzzer(true);
    buzzerPhaseStartedMs = now;
  }
}

void drawLcd() {
  if (!lcdReady || !lcdPowered) return;
  for (uint8_t row = 0; row < LCD_ROWS; row++) {
    lcd.setCursor(0, row);
    lcd.print(lcdRows[row]);  // always exactly 16 characters, clears old text
  }
}

void setLcdPower(bool on) {
  lcdPowered = on;
  if (!lcdReady) return;
  if (on) {
    lcd.display();
    lcd.backlight();
    drawLcd();
  } else {
    lcd.noBacklight();
    lcd.noDisplay();
  }
}

void setLcdText(const char* text) {
  memset(lcdRows[0], ' ', LCD_COLUMNS);
  memset(lcdRows[1], ' ', LCD_COLUMNS);
  lcdRows[0][LCD_COLUMNS] = '\0';
  lcdRows[1][LCD_COLUMNS] = '\0';
  uint8_t row = 0;
  uint8_t column = 0;
  for (const char* p = text; *p != '\0'; p++) {
    if (*p == '|' && row == 0) {
      row = 1;
      column = 0;
      continue;
    }
    if (column < LCD_COLUMNS && *p >= 32 && *p <= 126) {
      lcdRows[row][column++] = *p;
    }
  }
  drawLcd();
}

bool handleCommand(char* command) {
  if (strcmp(command, "H") == 0) return true;
  if (strcmp(command, "B,1") == 0) { setBuzzer(true); return true; }
  if (strcmp(command, "B,0") == 0) { setBuzzer(false); return true; }
  if (strcmp(command, "P,1") == 0) { setLcdPower(true); return true; }
  if (strcmp(command, "P,0") == 0) { setLcdPower(false); return true; }

  if (strncmp(command, "L,", 2) == 0) {
    setLcdText(command + 2);
    return true;
  }
  if (strncmp(command, "R,", 2) == 0) {
    int r, g, b;
    char extra;
    // Reject out-of-range values and trailing junk.
    if (sscanf(command + 2, "%d,%d,%d%c", &r, &g, &b, &extra) == 3 &&
        r >= 0 && r <= 255 && g >= 0 && g <= 255 &&
        b >= 0 && b <= 255) {
      setRgb((uint8_t)r, (uint8_t)g, (uint8_t)b);
      return true;
    }
  }
  return false;
}

void setup() {
  pinMode(RGB_R, OUTPUT);
  pinMode(RGB_G, OUTPUT);
  pinMode(RGB_B, OUTPUT);
  pinMode(BUZZER, OUTPUT);
  setRgb(0, 0, 0);
  setBuzzer(false);

  Serial.begin(115200);
  Wire.begin();
  lcdReady = (lcd.begin(LCD_COLUMNS, LCD_ROWS) == 0);
  if (lcdReady) {
    lcd.backlight();
    drawLcd();
  }
  lastMessageMs = millis();
}

void loop() {
  while (Serial.available() > 0) {
    char c = (char) Serial.read();
    if (c == '\r') continue;
    if (c == '\n') {
      if (!droppingLongLine && serialLength > 0) {
        serialBuffer[serialLength] = '\0';
        if (handleCommand(serialBuffer)) {
          linkActive = true;
          lastMessageMs = millis();
        }
      }
      serialLength = 0;
      droppingLongLine = false;
    } else if (!droppingLongLine) {
      if (serialLength < BUFFER_SIZE - 1) {
        serialBuffer[serialLength++] = c;
      } else {
        serialLength = 0;
        droppingLongLine = true;
      }
    }
  }

  // Fail-safe: a bridge crash must not leave the warning buzzer sounding.
  if (linkActive && (unsigned long)(millis() - lastMessageMs) > LINK_TIMEOUT_MS) {
    setBuzzer(false);
    setRgb(0, 0, 0);
    linkActive = false;
  }

  // Do not block serial input, LED changes, or LCD updates with delay().
  updateBuzzer();
}
