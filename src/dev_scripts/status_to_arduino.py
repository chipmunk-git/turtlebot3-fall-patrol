#!/usr/bin/env python3
"""Relay robot displays and popup-confirmed event alarms into ROS2 topics.

No microphone/speaker is required. Voice prompts are printed as text previews.
Run the Arduino serial bridge separately. The backend may be driven by fake_robot.py.
"""
import argparse
import json
import time
from urllib.error import URLError
from urllib.request import urlopen

import rclpy
from rclpy.node import Node
from rclpy.signals import SignalHandlerOptions
from std_msgs.msg import Bool, String

REFRESH_INTERVAL = 1.0

# LCD lines are ASCII and limited to 16 characters each for a 1602 display.
STATES = {
    "idle": ("0,0,255", "IDLE|STANDBY", None),
    "patrol": ("0,255,0", "MONITORING|PATROL ACTIVE", None),
    "observe": ("255,255,0", "OBSERVING|PLEASE WAIT", "괜찮으신가요? 도움이 필요하시면 도와주세요라고 말씀해 주세요."),
    "alert": ("255,0,0", "FALL SUSPECTED|STAFF CHECK", "낙상이 의심됩니다. 관리자에게 확인을 요청합니다."),
}

# Keep older API clients usable without adding display states or changing data.
STATE_ALIASES = {"suspect": "observe", "approach": "observe"}


def publish_twice(publisher, message):
    # This repeats an in-process publish to reduce transient command loss in a demo.
    # It is not an end-to-end delivery acknowledgement.
    publisher.publish(message)
    time.sleep(0.1)
    publisher.publish(message)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    url = args.server_url.rstrip("/") + "/status"
    alarm_url = args.server_url.rstrip("/") + "/events/alarms"

    # Python handles Ctrl+C; keep ROS2 alive until the OFF commands are sent.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    node = Node("tb3_mock_status_to_arduino")
    rgb_pub = node.create_publisher(String, "/tb3/rgb_led", 10)
    lcd_pub = node.create_publisher(String, "/tb3/lcd_text", 10)
    power_pub = node.create_publisher(Bool, "/tb3/lcd_power", 10)
    buzzer_pub = node.create_publisher(Bool, "/tb3/buzzer", 10)

    last_state = None
    last_publish = 0.0
    buzzer_on = False
    last_buzzer_publish = 0.0
    warned = False
    alarm_warned = False
    try:
        publish_twice(buzzer_pub, Bool(data=False))
        # Allow the local DDS participant to discover the long-running Arduino bridge.
        time.sleep(2.0)
        if not rgb_pub.get_subscription_count() or not lcd_pub.get_subscription_count():
            print("[주의] Arduino 브리지의 RGB/LCD 구독자를 아직 발견하지 못했습니다.", flush=True)
        publish_twice(power_pub, Bool(data=True))
        while True:
            status_ok = True
            try:
                with urlopen(url, timeout=1.0) as res:
                    payload = json.load(res)
                if not isinstance(payload, dict) or not isinstance(payload.get("state"), str):
                    raise ValueError("서버 상태 응답 형식이 올바르지 않습니다")
                state = payload["state"]
                state = STATE_ALIASES.get(state, state)
                warned = False
                now = time.monotonic()
                refresh_outputs = state != last_state or now - last_publish >= REFRESH_INTERVAL
                if refresh_outputs:
                    if state in STATES:
                        rgb, lcd, spoken = STATES[state]
                        publish_twice(rgb_pub, String(data=rgb))
                        publish_twice(lcd_pub, String(data=lcd))
                        publish_twice(power_pub, Bool(data=True))
                        last_publish = now
                        if state != last_state:
                            print(f"[상태] {state:<8} LED={rgb} LCD={lcd}", flush=True)
                            if spoken:
                                print(f"[가상 스피커] {spoken}", flush=True)
                    elif state != last_state:
                        print(f"[주의] 알 수 없는 상태: {state!r}", flush=True)
                    last_state = state
            except (URLError, TimeoutError, OSError, KeyError, ValueError) as error:
                status_ok = False
                if not warned:
                    print(f"[서버 통신 실패] {error}", flush=True)
                    warned = True
            if status_ok:
                try:
                    with urlopen(alarm_url, timeout=1.0) as res:
                        alarm = json.load(res)
                    ids = alarm.get("active_event_ids") if isinstance(alarm, dict) else None
                    if not isinstance(ids, list) or any(type(i) is not int or i <= 0 for i in ids):
                        raise ValueError("사건 경보 응답 형식이 올바르지 않습니다")
                    next_buzzer = bool(ids)
                    alarm_warned = False
                    if next_buzzer != buzzer_on or now - last_buzzer_publish >= REFRESH_INTERVAL:
                        publish_twice(buzzer_pub, Bool(data=next_buzzer))
                        buzzer_on = next_buzzer
                        last_buzzer_publish = now
                except (URLError, TimeoutError, OSError, KeyError, ValueError) as error:
                    publish_twice(buzzer_pub, Bool(data=False))
                    buzzer_on = False
                    if not alarm_warned:
                        print(f"[사건 경보 조회 실패] {error}", flush=True)
                        alarm_warned = True
            else:
                publish_twice(buzzer_pub, Bool(data=False))
                buzzer_on = False
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n[종료] 부저·LED·LCD 화면을 끕니다.", flush=True)
    finally:
        publish_twice(buzzer_pub, Bool(data=False))
        publish_twice(rgb_pub, String(data="0,0,0"))
        publish_twice(lcd_pub, String(data=""))
        publish_twice(power_pub, Bool(data=False))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
