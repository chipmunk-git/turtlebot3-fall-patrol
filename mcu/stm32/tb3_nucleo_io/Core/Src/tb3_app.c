/*
 * TurtleBot3 NUCLEO-F103RB STM32CubeIDE / HAL test adaptation.
 * Based on the command protocol of arduino/tb3_io/tb3_io.ino:
 *   R,255,0,0  RGB ; L,top|bottom  LCD ; P,1/P,0 LCD power
 *   H          heartbeat ; B,1/B,0   buzzer start / stop
 * UART: ST-LINK VCP USART2 115200 8N1, newline terminated.
 * RGB/LCD validated on physical NUCLEO; active buzzer GPIO control updated.
 *
 * Required CubeMX handles: hi2c1, huart2, htim1, htim2, htim4
 * TIM1_CH1 PA8 (D7), GPIO PA9 (D8, active buzzer), TIM2_CH3 PB10 (D6, TIM2 partial remap 2),
 * TIM4_CH1 PB6 (D10), I2C1 PB8/PB9 (remapped), USART2 PA2/PA3.
 * LCD: common PCF8574 backpack, RS=P0 RW=P1 EN=P2 BL=P3 D4..D7=P4..P7.
 */

#include "main.h"
#include "tb3_app.h"
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

extern UART_HandleTypeDef huart2;
extern I2C_HandleTypeDef hi2c1;
extern TIM_HandleTypeDef htim1;
extern TIM_HandleTypeDef htim2;
extern TIM_HandleTypeDef htim4;

#define UART_RING_SIZE 256u
#define CMD_MAX_LEN 70u
#define LINK_TIMEOUT_MS 4000u
#define BUZZER_BEEP_ON_MS 150u
#define BUZZER_BEEP_OFF_MS 600u
#define LCD_WIDTH 16u
#define LCD_RS 0x01u
#define LCD_E  0x04u
#define LCD_BL 0x08u

/* Active buzzer (2 pin): PA9 D8 -> 220-ohm resistor -> buzzer (+), buzzer (-) -> GND.
   GPIO HIGH enables the buzzer; GPIO LOW makes its two terminals ~0 V.
   This application does not start TIM1_CH2. The firmware reconfigures PA9 to
   GPIO output even if an older CubeMX .ioc still lists it as TIM1_CH2.
   WARNING: a 220-ohm resistor limits current to at most 15 mA at 3.3 V,
   but does not prove an 8-mA GPIO operating point or suppress inductive spikes.
   Do not connect the buzzer directly to a 5-V source and PA9 together. */

static volatile uint8_t rx_byte;
static volatile uint8_t uart_ring[UART_RING_SIZE];
static volatile uint16_t rx_head = 0;
static volatile uint16_t rx_tail = 0;
static char cmd_buf[CMD_MAX_LEN];
static uint8_t cmd_len = 0;
static bool dropping_line = false;
static bool link_active = false;
static uint32_t last_valid_ms = 0;
static bool buzzer_on = false;
static bool buzzer_sounding = false;
static uint32_t buzzer_phase_started_ms = 0;
static bool lcd_ready = false;
static bool lcd_visible = true;
static uint16_t lcd_addr = 0; /* 8-bit HAL address: 7-bit address << 1 */
static char lcd_lines[2][LCD_WIDTH + 1] = {
  "WAIT FOR ROS2   ",
  "                "
};

/* HAL TIM3 is not used; RGB timers intentionally separated. */
static void pwm_value(TIM_HandleTypeDef *timer, uint32_t channel, uint8_t value)
{
  const uint32_t period = __HAL_TIM_GET_AUTORELOAD(timer);
  uint32_t compare = ((period + 1u) * (uint32_t)value) / 255u;
  if (compare > period) compare = period;
  __HAL_TIM_SET_COMPARE(timer, channel, compare);
}

static void rgb_set(uint8_t red, uint8_t green, uint8_t blue)
{
  /* Expected COMMON CATHODE RGB module, each color has current limiting resistor. */
  pwm_value(&htim1, TIM_CHANNEL_1, red);    /* PA8 D7 */
  pwm_value(&htim2, TIM_CHANNEL_3, green);  /* PB10 D6 */
  pwm_value(&htim4, TIM_CHANNEL_1, blue);   /* PB6 D10 */
}

/* A powered active buzzer makes its own tone; only DC ON/OFF is required. */
static void buzzer_drive(bool sounding)
{
  HAL_GPIO_WritePin(GPIOA, GPIO_PIN_9,
                    sounding ? GPIO_PIN_SET : GPIO_PIN_RESET);
  buzzer_sounding = sounding;
}

static void buzzer_set(bool on)
{
  if (!on) {
    buzzer_on = false;
    buzzer_drive(false);
    return;
  }
  if (buzzer_on) return; /* Repeated B,1 must not restart the beep cycle. */
  buzzer_on = true;
  buzzer_phase_started_ms = HAL_GetTick();
  buzzer_drive(true);
}

static void buzzer_update(void)
{
  if (!buzzer_on) return;
  const uint32_t now = HAL_GetTick();
  const uint32_t elapsed = now - buzzer_phase_started_ms;
  if (buzzer_sounding && elapsed >= BUZZER_BEEP_ON_MS) {
    buzzer_drive(false);
    buzzer_phase_started_ms = now;
  } else if (!buzzer_sounding && elapsed >= BUZZER_BEEP_OFF_MS) {
    buzzer_drive(true);
    buzzer_phase_started_ms = now;
  }
}

static bool lcd_raw(uint8_t nibble, uint8_t flags)
{
  if (lcd_addr == 0) return false;
  const uint8_t base = (uint8_t)((nibble << 4) | flags | (lcd_visible ? LCD_BL : 0u));
  uint8_t packet[2] = { (uint8_t)(base | LCD_E), base };
  return HAL_I2C_Master_Transmit(&hi2c1, lcd_addr, packet, 2, 40) == HAL_OK;
}

static void lcd_send(uint8_t value, bool rs)
{
  (void)lcd_raw((uint8_t)(value >> 4), rs ? LCD_RS : 0u);
  (void)lcd_raw((uint8_t)(value & 0x0Fu), rs ? LCD_RS : 0u);
}

static void lcd_cmd(uint8_t cmd)
{
  lcd_send(cmd, false);
  if (cmd == 0x01u || cmd == 0x02u) HAL_Delay(2);
}

static void lcd_write_char(char c)
{
  lcd_send((uint8_t)c, true);
}

static void lcd_draw(void)
{
  if (!lcd_ready || !lcd_visible) return;
  for (uint8_t row = 0; row < 2; ++row) {
    lcd_cmd((uint8_t)(row == 0 ? 0x80u : 0xC0u));
    for (uint8_t col = 0; col < LCD_WIDTH; ++col) {
      lcd_write_char(lcd_lines[row][col]);
    }
  }
}

static void lcd_set_text(const char *s)
{
  memset(lcd_lines[0], ' ', LCD_WIDTH);
  memset(lcd_lines[1], ' ', LCD_WIDTH);
  lcd_lines[0][LCD_WIDTH] = '\0';
  lcd_lines[1][LCD_WIDTH] = '\0';
  uint8_t row = 0, col = 0;
  while (*s) {
    unsigned char c = (unsigned char)*s++;
    if (c == '|' && row == 0) { row = 1; col = 0; continue; }
    if (c >= 32 && c <= 126 && col < LCD_WIDTH) lcd_lines[row][col++] = (char)c;
  }
  lcd_draw();
}

static void lcd_set_visible(bool on)
{
  lcd_visible = on;
  if (!lcd_ready) return;
  lcd_cmd(on ? 0x0Cu : 0x08u); /* display on/off */
  if (on) lcd_draw();
}

static void lcd_init(void)
{
  /* Common I2C backpack address candidates PCF8574T=0x27, PCF8574AT=0x3F.
     Other backpack chips and layouts are NOT supported by this minimal driver. */
  const uint8_t candidates[] = {0x27u, 0x3Fu};
  for (unsigned i = 0; i < sizeof(candidates); i++) {
    const uint16_t address = (uint16_t)candidates[i] << 1;
    if (HAL_I2C_IsDeviceReady(&hi2c1, address, 2, 100) == HAL_OK) {
      lcd_addr = address;
      break;
    }
  }
  if (!lcd_addr) return;
  HAL_Delay(50);
  (void)lcd_raw(0x03u, 0); HAL_Delay(5);
  (void)lcd_raw(0x03u, 0); HAL_Delay(5);
  (void)lcd_raw(0x03u, 0); HAL_Delay(1);
  (void)lcd_raw(0x02u, 0); HAL_Delay(1);
  lcd_cmd(0x28u); /* 4 bit, 2 line */
  lcd_cmd(0x08u); /* display off during init */
  lcd_cmd(0x01u); /* clear */
  lcd_cmd(0x06u); /* increment cursor */
  lcd_cmd(0x0Cu); /* display on */
  lcd_ready = true;
  lcd_draw();
}

static bool dispatch(char *cmd)
{
  if (strcmp(cmd, "H") == 0) return true;
  if (strcmp(cmd, "B,0") == 0) { buzzer_set(false); return true; }
  if (strcmp(cmd, "B,1") == 0) { buzzer_set(true); return true; }
  if (strcmp(cmd, "P,0") == 0) { lcd_set_visible(false); return true; }
  if (strcmp(cmd, "P,1") == 0) { lcd_set_visible(true); return true; }
  if (strncmp(cmd, "L,", 2) == 0) { lcd_set_text(cmd + 2); return true; }
  if (strncmp(cmd, "R,", 2) == 0) {
    int red = -1, green = -1, blue = -1;
    char extra = 0;
    if (sscanf(cmd + 2, "%d,%d,%d%c", &red, &green, &blue, &extra) == 3 &&
        red >= 0 && red <= 255 && green >= 0 && green <= 255 &&
        blue >= 0 && blue <= 255) {
      rgb_set((uint8_t)red, (uint8_t)green, (uint8_t)blue);
      return true;
    }
  }
  return false;
}

static void input_char(char c)
{
  if (c == '\r') return;
  if (c == '\n') {
    if (!dropping_line && cmd_len > 0) {
      cmd_buf[cmd_len] = '\0';
      if (dispatch(cmd_buf)) {
        link_active = true;
        last_valid_ms = HAL_GetTick();
      }
    }
    cmd_len = 0;
    dropping_line = false;
    return;
  }
  if (!dropping_line) {
    if (cmd_len < CMD_MAX_LEN - 1u) cmd_buf[cmd_len++] = c;
    else { cmd_len = 0; dropping_line = true; }
  }
}

void HAL_UART_RxCpltCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance != USART2) return;
  const uint16_t next = (uint16_t)((rx_head + 1u) % UART_RING_SIZE);
  if (next != rx_tail) { uart_ring[rx_head] = rx_byte; rx_head = next; }
  /* Keep receiving while LCD I2C drawing runs in the main loop. */
  (void)HAL_UART_Receive_IT(&huart2, (uint8_t *)&rx_byte, 1);
}

void HAL_UART_ErrorCallback(UART_HandleTypeDef *huart)
{
  if (huart->Instance == USART2) {
    (void)HAL_UART_Receive_IT(&huart2, (uint8_t *)&rx_byte, 1);
  }
}

void TB3_App_Init(void)
{
  /* CubeMX must initialize TIM1/TIM2/TIM4, I2C1, USART2 before this call.
     PA9 is deliberately reconfigured here; no generated main.c edits needed. */
  __HAL_RCC_GPIOA_CLK_ENABLE();
  HAL_GPIO_WritePin(GPIOA, GPIO_PIN_9, GPIO_PIN_RESET); /* preload LOW */
  GPIO_InitTypeDef GPIO_InitStruct = {0};
  GPIO_InitStruct.Pin = GPIO_PIN_9;
  GPIO_InitStruct.Mode = GPIO_MODE_OUTPUT_PP;
  GPIO_InitStruct.Pull = GPIO_NOPULL;
  GPIO_InitStruct.Speed = GPIO_SPEED_FREQ_LOW;
  HAL_GPIO_Init(GPIOA, &GPIO_InitStruct);
  buzzer_drive(false); /* explicit LOW also after GPIO configuration */

  (void)HAL_TIM_PWM_Start(&htim1, TIM_CHANNEL_1); /* RGB red only */
  (void)HAL_TIM_PWM_Start(&htim2, TIM_CHANNEL_3);
  (void)HAL_TIM_PWM_Start(&htim4, TIM_CHANNEL_1);
  rgb_set(0, 0, 0);
  (void)HAL_UART_Receive_IT(&huart2, (uint8_t *)&rx_byte, 1);
  lcd_init();
  last_valid_ms = HAL_GetTick();
}

void TB3_App_Loop(void)
{
  while (rx_tail != rx_head) {
    const uint8_t ch = uart_ring[rx_tail];
    rx_tail = (uint16_t)((rx_tail + 1u) % UART_RING_SIZE);
    input_char((char)ch);
  }
  buzzer_update();
  if (link_active && (uint32_t)(HAL_GetTick() - last_valid_ms) > LINK_TIMEOUT_MS) {
    rgb_set(0, 0, 0);
    buzzer_set(false); /* No heartbeat or other valid command: fail silent. */
    link_active = false;
  }
}
