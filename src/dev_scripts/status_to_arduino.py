#!/usr/bin/env python3
"""Prototype: relay backend robot state into existing RGB LED/LCD ROS2 topics.

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
from std_msgs.msg import Bool, String

# LCD lines are ASCII and limited to 16 characters each for a 1602 display.
STATES = {
    "idle": ("0,0,0", "IDLE|STANDBY", None),
    "patrol": ("0,255,0", "MONITORING|PATROL ACTIVE", None),
    "suspect": ("255,255,0", "PERSON DETECTED|CHECKING", None),
    "approach": ("0,0,255", "APPROACHING|KEEP DISTANCE", None),
    "observe": ("255,255,0", "OBSERVING|PLEASE WAIT", "괜찮으신가요? 도움이 필요하시면 도와주세요라고 말씀해 주세요."),
    "alert": ("255,0,0", "FALL SUSPECTED|STAFF CHECK", "낙상이 의심됩니다. 관리자에게 확인을 요청합니다."),
}


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

    rclpy.init()
    node = Node("tb3_mock_status_to_arduino")
    rgb_pub = node.create_publisher(String, "/tb3/rgb_led", 10)
    lcd_pub = node.create_publisher(String, "/tb3/lcd_text", 10)
    power_pub = node.create_publisher(Bool, "/tb3/lcd_power", 10)

    # Allow the local DDS participant to discover the long-running Arduino bridge.
    time.sleep(2.0)
    if not rgb_pub.get_subscription_count() or not lcd_pub.get_subscription_count():
        print("[주의] Arduino 브리지의 RGB/LCD 구독자를 아직 발견하지 못했습니다.", flush=True)
    publish_twice(power_pub, Bool(data=True))
    last_state = None
    warned = False
    try:
        while True:
            try:
                with urlopen(url, timeout=1.0) as res:
                    state = json.load(res)["state"]
                warned = False
                if state != last_state:
                    if state in STATES:
                        rgb, lcd, spoken = STATES[state]
                        publish_twice(rgb_pub, String(data=rgb))
                        publish_twice(lcd_pub, String(data=lcd))
                        print(f"[상태] {state:<8} LED={rgb} LCD={lcd}", flush=True)
                        if spoken:
                            print(f"[가상 스피커] {spoken}", flush=True)
                    else:
                        print(f"[주의] 알 수 없는 상태: {state!r}", flush=True)
                    last_state = state
            except (URLError, TimeoutError, OSError, KeyError, ValueError) as error:
                if not warned:
                    print(f"[서버 통신 실패] {error}", flush=True)
                    warned = True
            time.sleep(0.4)
    except KeyboardInterrupt:
        print("\n[종료] LED와 LCD 화면을 끕니다.", flush=True)
    finally:
        publish_twice(rgb_pub, String(data="0,0,0"))
        publish_twice(lcd_pub, String(data=""))
        publish_twice(power_pub, Bool(data=False))
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    main()
