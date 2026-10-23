"""ROS2 Humble topics -> Arduino Uno serial bridge for RGB, buzzer and LCD."""

import re
import time

import rclpy
from rclpy.node import Node
from std_msgs.msg import Bool, String
import serial


class ArduinoBridge(Node):
    def __init__(self):
        super().__init__('tb3_arduino_bridge')
        self.declare_parameter('port', '/dev/ttyACM0')
        self.declare_parameter('baudrate', 115200)
        self.port = self.get_parameter('port').value
        self.baudrate = int(self.get_parameter('baudrate').value)

        self.serial_device = None
        self.ready_after = 0.0
        self.last_connect_attempt = -999.0
        self.last_heartbeat = 0.0

        self.rgb = (0, 0, 0)
        self.buzzer = False
        self.lcd_text = 'ROS2 READY|TurtleBot3'
        self.lcd_power = True
        self.pending = {'B', 'R', 'L', 'P'}

        self.create_subscription(String, '/tb3/rgb_led', self.on_rgb, 10)
        self.create_subscription(Bool, '/tb3/buzzer', self.on_buzzer, 10)
        self.create_subscription(String, '/tb3/lcd_text', self.on_lcd_text, 10)
        self.create_subscription(Bool, '/tb3/lcd_power', self.on_lcd_power, 10)
        self.create_timer(0.2, self.tick)
        self.get_logger().info(f'Waiting for Arduino serial device: {self.port}')

    def on_rgb(self, msg):
        value = msg.data.strip()
        if value.lower() == 'off':
            rgb = (0, 0, 0)
        elif re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            rgb = tuple(bytes.fromhex(value[1:]))
        elif re.fullmatch(r'[0-9]{1,3},[0-9]{1,3},[0-9]{1,3}', value):
            rgb = tuple(int(x) for x in value.split(','))
            if any(x > 255 for x in rgb):
                self.get_logger().warning('RGB values must be 0..255')
                return
        else:
            self.get_logger().warning('RGB: use "255,0,0", "#FF0000", or "off"')
            return
        self.rgb = rgb
        self.pending.add('R')

    def on_buzzer(self, msg):
        self.buzzer = bool(msg.data)
        self.pending.add('B')

    def on_lcd_text(self, msg):
        if any(not (32 <= ord(ch) <= 126) for ch in msg.data):
            self.get_logger().warning('LCD text must use printable ASCII only (no newlines/Korean)')
            return
        rows = msg.data.split('|', 1)
        first = rows[0][:16]
        second = rows[1][:16] if len(rows) == 2 else ''
        self.lcd_text = first + '|' + second
        self.pending.add('L')

    def on_lcd_power(self, msg):
        self.lcd_power = bool(msg.data)
        self.pending.add('P')

    def disconnect(self):
        if self.serial_device is not None:
            try:
                self.serial_device.close()
            except Exception:
                pass
            self.serial_device = None
        self.pending.update(('B', 'R', 'L', 'P'))

    def send(self, line):
        try:
            self.serial_device.write((line + '\n').encode('ascii'))
            return True
        except (serial.SerialException, OSError) as exc:
            self.get_logger().warning(f'Serial connection lost: {exc}')
            self.disconnect()
            return False

    def tick(self):
        now = time.monotonic()
        if self.serial_device is None:
            if now - self.last_connect_attempt < 2.0:
                return
            self.last_connect_attempt = now
            try:
                self.serial_device = serial.Serial(
                    self.port, baudrate=self.baudrate,
                    timeout=0, write_timeout=0.5,
                )
            except (serial.SerialException, OSError) as exc:
                self.get_logger().warning(f'Unable to open {self.port}: {exc}')
                return
            # Opening USB serial often resets the Uno. Wait for the bootloader.
            self.ready_after = now + 2.5
            self.pending.update(('B', 'R', 'L', 'P'))
            self.get_logger().info(f'Serial connected; waiting for Uno boot: {self.port}')
            return

        if now < self.ready_after:
            return

        commands = {
            'B': f'B,{int(self.buzzer)}',
            'R': f'R,{self.rgb[0]},{self.rgb[1]},{self.rgb[2]}',
            'L': 'L,' + self.lcd_text,
            'P': f'P,{int(self.lcd_power)}',
        }
        # Send pending state first. Keep unsent commands pending after a failure.
        for name in ('B', 'R', 'L', 'P'):
            if name in self.pending:
                if not self.send(commands[name]):
                    return
                self.pending.discard(name)

        if now - self.last_heartbeat >= 1.0:
            if self.send('H'):
                self.last_heartbeat = now

    def stop(self):
        if self.serial_device is not None:
            # Best effort: ensure outputs are not left on after Ctrl+C.
            self.send('B,0')
            self.send('R,0,0,0')
        self.disconnect()


def main(args=None):
    rclpy.init(args=args)
    node = ArduinoBridge()
    try:
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        node.stop()
        node.destroy_node()
        rclpy.shutdown()


if __name__ == '__main__':
    main()
