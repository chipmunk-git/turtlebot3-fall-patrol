# MCU 출력 장치 펌웨어

Arduino Uno와 STM32 NUCLEO-F103RB에서 RGB LED·LCD·부저를 제어하는 펌웨어입니다. 두 보드는 같은 USB 시리얼 명령을 사용합니다.

```text
mcu/
├── arduino/tb3_io/tb3_io.ino
└── stm32/tb3_nucleo_io/
    ├── .project / .cproject / .mxproject
    ├── .settings/org.eclipse.core.resources.prefs
    ├── tb3_nucleo_io.ioc
    ├── STM32F103RBTX_FLASH.ld
    ├── Core/                    # 응용 코드·초기화·인터럽트·시작 코드
    └── Drivers/                 # HAL·CMSIS 및 라이선스 파일
```

## 핀 연결

| 기능 | Arduino Uno | STM32 NUCLEO-F103RB |
|---|---|---|
| RGB 빨강 | D5 | D7 / PA8 / TIM1_CH1 |
| RGB 초록 | D6 | D6 / PB10 / TIM2_CH3 |
| RGB 파랑 | D9 | D10 / PB6 / TIM4_CH1 |
| 부저 | D8 | D8 / PA9 / GPIO 출력 |
| LCD I²C SCL | A5 | D15 / PB8 / I2C1_SCL |
| LCD I²C SDA | A4 | D14 / PB9 / I2C1_SDA |
| USB 시리얼 | 보드 USB의 Serial | ST-LINK USB VCP / USART2 (TX PA2, RX PA3) |

두 보드의 핀 번호와 부저 구동 방식은 다릅니다. Uno 스케치는 기본적으로 수동 부저에 `tone()`을 사용하며 설정 상수로 종류를 바꿀 수 있습니다. STM32 코드는 능동 부저를 GPIO HIGH/LOW로 제어합니다. RGB는 기본적으로 공통 캐소드 모듈을 전제로 합니다.

STM32는 TIM2 partial remap 2와 I2C1 remap을 사용합니다. `.ioc`에는 PA9가 TIM1_CH2로 남아 있지만, `TB3_App_Init()`에서 GPIO 출력으로 다시 설정하며 TIM1_CH2 PWM은 시작하지 않습니다. 보드 설정을 재생성할 때도 이 초기화 순서를 유지합니다.

## 시리얼 명령

115200 baud, 8N1이며 각 명령 끝에 줄바꿈(`\n`)을 붙입니다. LCD 문자는 표시 가능한 ASCII, 한 줄 최대 16자입니다.

| 명령 예 | 동작 |
|---|---|
| `R,255,0,0` | RGB 밝기 설정 (각 값 0~255) |
| `L,Hello|TurtleBot3` | LCD 첫째 줄 / 둘째 줄 |
| `P,1` / `P,0` | LCD 표시·백라이트 켜기 / 끄기 |
| `B,1` / `B,0` | 부저 반복음 시작 / 정지 |
| `H` | 통신 유지 신호 |

부저 반복음은 150ms 켜짐 → 600ms 꺼짐입니다. 유효 명령을 받은 뒤 4초 이상 추가 명령이 없으면 RGB와 부저를 끕니다. LCD는 이 시간 초과 처리로 끄지 않습니다.

## Arduino Uno

Arduino IDE에서 [tb3_io.ino](arduino/tb3_io/tb3_io.ino)를 열고 Uno 보드와 실제 USB 포트를 선택합니다. Uno용 보드 패키지와 라이브러리 관리자에서 **hd44780 by Bill Perry**를 준비한 뒤 빌드·업로드합니다.

## STM32 NUCLEO-F103RB

STM32CubeIDE에서 **File → Import → General → Existing Projects into Workspace**로 `mcu/stm32/tb3_nucleo_io/`를 선택합니다. IDE의 컴파일러·빌드 도구가 준비된 환경에서 프로젝트 이름 `tb3_nucleo_io`를 유지하고 빌드합니다. 보드 업로드·디버깅은 ST-LINK 연결을 확인하고 해당 PC에서 디버그 구성을 생성합니다.

`Debug/`·`Release/`는 빌드 중 다시 생성됩니다. 개인 디버그 구성인 `.launch`와 IDE의 개인 UI·인덱싱 설정은 공유 대상에서 제외합니다. `.project`·`.cproject`는 Git에 포함합니다.

기존 소스를 빌드할 때 필요한 HAL·CMSIS는 `Drivers/`에 포함되어 있습니다. `.ioc`에서 핀·주변장치 초기화 코드를 다시 생성하려면 STM32CubeMX와 프로젝트에 지정된 **STM32Cube FW_F1 V1.8.7**을 준비합니다. 코드를 재생성했다면 아래 응용 함수 연결과 PA9 설정을 확인합니다.

| 핵심 파일 | 역할 |
|---|---|
| [Core/Src/main.c](stm32/tb3_nucleo_io/Core/Src/main.c) | 주변장치 초기화 및 응용 함수 호출 |
| [Core/Src/tb3_app.c](stm32/tb3_nucleo_io/Core/Src/tb3_app.c) | 명령 해석·RGB·LCD·부저 제어 |
| [Core/Inc/tb3_app.h](stm32/tb3_nucleo_io/Core/Inc/tb3_app.h) | 응용 함수 선언 |
| [Core/Src/stm32f1xx_hal_msp.c](stm32/tb3_nucleo_io/Core/Src/stm32f1xx_hal_msp.c) | 핀·리맵·주변장치 클록·인터럽트 설정 |
| [Core/Src/stm32f1xx_it.c](stm32/tb3_nucleo_io/Core/Src/stm32f1xx_it.c) | USART2 인터럽트를 HAL로 전달 |

`main.c`에는 다음 연결이 이미 들어 있습니다. 같은 코드를 중복 추가하지 않습니다.

```c
/* USER CODE BEGIN Includes */
#include "tb3_app.h"

/* USER CODE BEGIN 2: 주변장치 초기화 후 */
TB3_App_Init();

/* USER CODE BEGIN 3: while (1) 내부 */
TB3_App_Loop();
```

## 서버·ROS2 연결과 확인 범위

기존 [tb3_arduino_io 브리지](../src/tb3_arduino_io/tb3_arduino_io/bridge.py)는 위 시리얼 명령을 보냅니다. 실행 컴퓨터·USB 포트·서버 주소 설정은 [최상위 README](../README.md#빠른-실행-웹-상태--arduino-표시-장치)와 [통합 실행 안내](../src/my_bot_fall/README.md)를 참고합니다.

핀·초기화·명령 처리는 소스 기준으로 확인했습니다. 이 위치의 STM32 프로젝트를 다른 PC에서 가져와 빌드·업로드하는 절차와 서버→ROS2 브리지→실제 보드의 전체 동작은 실기 확인이 필요합니다. `status_to_arduino.py`는 RGB·LCD를 갱신하지만 부저를 자동 제어하지 않습니다.
