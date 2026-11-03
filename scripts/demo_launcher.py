"""Start the HANDOVER.md sequence in separate tmux terminals, including Pi SSH jobs."""

import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
SOCKET = "turtlewatch-demo"
SESSION = "demo"
NO_EXISTING_DRIVER = r"""
if info=$(ros2 topic info /cmd_vel 2>/dev/null); then
    if ! [[ "$info" =~ Publisher\ count:\ 0([^0-9]|$) ]]; then
        echo "기존 /cmd_vel 발행자가 있습니다. 주행 프로그램을 먼저 종료하세요." >&2
        exit 1
    fi
fi
"""


def quote(value):
    return shlex.quote(str(value))


def shell_path(value):
    if value.startswith("~/"):
        return '"$HOME"/' + quote(value[2:])
    if not value.startswith("/"):
        raise ValueError("작업공간 경로는 절대 경로 또는 ~/로 지정하세요.")
    return quote(value)


@dataclass
class Job:
    name: str
    host: str
    window: int
    command: str
    probe: str


def environment(config, host, overlay="project"):
    workspace = config["pc_root"] if host == "pc" else config["pi_root"]
    # Each terminal starts from its own ROS environment, outside the backend venv.
    lines = [
        "set -e",
        "unset AMENT_PREFIX_PATH COLCON_PREFIX_PATH CMAKE_PREFIX_PATH PYTHONPATH VIRTUAL_ENV PYTHONHOME",
        "export PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        "source /opt/ros/humble/setup.bash",
        "export ROS_LOCALHOST_ONLY=0",
        "export ROS_DOMAIN_ID=" + quote(config["domain"]),
        "export TURTLEBOT3_MODEL=waffle_pi",
        "export LDS_MODEL=LDS-02",
        "export PYTHONUNBUFFERED=1",
    ]
    if config.get("rmw"):
        lines.append("export RMW_IMPLEMENTATION=" + quote(config["rmw"]))
    if overlay == "robot":
        lines.append('if [[ -f "$HOME/turtlebot3_ws/install/setup.bash" ]]; then source "$HOME/turtlebot3_ws/install/setup.bash"; fi')
    elif overlay == "project":
        lines.append("source " + shell_path(workspace + "/install/setup.bash"))
    lines.append("cd " + shell_path(workspace))
    return "\n".join(lines) + "\n"


def topic_probe(topic, message_type, field="header", transient=False):
    durability = "transient_local" if transient else "volatile"
    reliability = "reliable" if transient else "best_effort"
    return ("timeout 5 ros2 topic echo " + quote(topic) + " " + quote(message_type)
            + " --once --qos-durability " + durability + " --qos-reliability "
            + reliability + " --field " + quote(field))


def node_probe(name):
    return "ros2 node list | grep -Fx -- " + quote(name)


def device_command(override, pattern, label):
    if override:
        return "device=" + quote(override) + '\n[[ -r "$device" && -w "$device" ]]\n'
    return ("shopt -s nullglob\ndevices=(" + pattern + ")\n"
            + '[[ ${#devices[@]} == 1 ]] || { echo '
            + quote(label + " 장치를 하나로 특정하지 못했습니다. 포트를 직접 지정하세요.")
            + ' >&2; exit 1; }\ndevice=${devices[0]}\n[[ -r "$device" && -w "$device" ]]\n')


def build_jobs(config):
    pc = environment(config, "pc")
    pi = environment(config, "pi")
    robot = environment(config, "pi", "robot")
    plain = environment(config, "pc", "none")
    base = "http://127.0.0.1:8000"
    health = "curl -fsS --max-time 3 " + quote(base + "/health")
    health += " | /usr/bin/python3 -c " + quote("import json,sys; assert json.load(sys.stdin).get('status') == 'ok'")
    pose_check = (
        "import json, sys, yaml\n"
        "value = next(yaml.safe_load_all(sys.stdin))\n"
        "data = json.loads(value) if isinstance(value, str) else value\n"
        "assert isinstance(data, dict) and data.get('camera_status') == 'ok'\n"
    )
    return [
        Job("bringup", "pi", 9, robot + device_command(config["opencr"],
            "/dev/serial/by-id/usb-ROBOTIS_OpenCR*", "OpenCR")
            + 'exec ros2 launch turtlebot3_bringup robot.launch.py usb_port:="$device"',
            plain + topic_probe("/odom", "nav_msgs/msg/Odometry") + "\n"
            + topic_probe("/scan", "sensor_msgs/msg/LaserScan") + "\n"
            + 'info=$(ros2 topic info /cmd_vel)\n[[ "$info" =~ Subscription\\ count:\\ 1([^0-9]|$) ]]'),
        Job("camera", "pi", 10, robot
            + 'exec ros2 run v4l2_camera v4l2_camera_node --ros-args -p image_size:="[320,240]"'
            + ' -p time_per_frame:="[1,15]" -p image_raw.jpeg_quality:=50',
            plain + topic_probe("/image_raw/compressed", "sensor_msgs/msg/CompressedImage")),
        Job("server", "pc", 4, plain
            + "export FALL_WHISPER_CLI=" + shell_path(config["whisper_cli"]) + "\n"
            + "export FALL_WHISPER_MODEL=" + shell_path(config["model"]) + "\n"
            + "cd " + quote(config["pc_root"] + "/backend") + "\n"
            + "exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1",
            plain + health),
        Job("bridge", "pi", 5, pi + device_command(config["mcu"],
            "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink*", "NUCLEO")
            + 'exec ros2 run tb3_arduino_io bridge --ros-args -p port:="$device"',
            plain + node_probe("/tb3_arduino_bridge")),
        Job("voice", "pi", 3, pi
            + "amixer -c " + quote(config["speaker_card"]) + " sset PCM 60%\n"
            + "exec ros2 run my_bot_fall voice --ros-args -p script_path:="
            + shell_path(config["pi_root"] + "/voice/stt/whisper_session.py")
            + " -p server_url:=" + quote(config["server_url"])
            + " -p input_device:=" + quote(config["input_device"])
            + " -p output_device:=" + quote(config["output_device"]),
            pi + "curl -fsS --max-time 3 " + quote(config["server_url"] + "/health")
            + "\n" + node_probe("/fall_patrol_voice")),
        Job("outputs", "pc", 6, pc
            + "exec /usr/bin/python3 src/dev_scripts/status_to_arduino.py --server-url " + base,
            plain + node_probe("/tb3_mock_status_to_arduino")),
        Job("localization", "pc", 7, pc
            + "exec ros2 launch my_bot_fall tb3_localization_real.launch.py map:="
            + quote(config["pc_root"] + "/src/maps/new_map.yaml") + " use_sim_time:=false",
            pc + "ros2 lifecycle get /map_server | grep -q '^active'\n"
            + "ros2 lifecycle get /amcl | grep -q '^active'\n"
            + node_probe("/rviz2") + "\n"
            + topic_probe("/map", "nav_msgs/msg/OccupancyGrid", "info", True)),
        Job("video", "pc", 8, plain + "exec ros2 run web_video_server web_video_server",
            "curl -fsS --max-time 3 http://127.0.0.1:8080/ >/dev/null"),
        Job("detector", "pc", 2, pc
            + 'exec ros2 run my_bot_fall lying_detector --ros-args --params-file "$(ros2 pkg prefix my_bot_fall)/share/my_bot_fall/config/params_robot.yaml"',
            plain + topic_probe("/pose_state", "std_msgs/msg/String", "data")
            + " | /usr/bin/python3 -c " + quote(pose_check)),
        Job("patrol", "pc", 1, pc
            + "exec ros2 run my_bot_fall patrol --ros-args -p server_url:=" + base
            + " -p voice_enabled:=true -p waypoint_count:=3 -p linear_speed:=0.15",
            plain + node_probe("/astar_driver_yolo") + "\n"
            + topic_probe("/fall_patrol/state", "std_msgs/msg/String", "data")),
    ]


class Launcher:
    def __init__(self, config, run=subprocess.run, clock=time.monotonic, sleep=time.sleep):
        self.config = config
        self.run = run
        self.clock = clock
        self.sleep = sleep
        self.directory = Path(config["run_dir"])
        self.jobs = build_jobs(config)
        self.panes = config.setdefault("panes", {})

    def tmux(self, *args, check=True):
        result = self.run(["tmux", "-L", SOCKET, "-f", "/dev/null", *args],
                          capture_output=True, text=True, timeout=10)
        if check and result.returncode:
            raise RuntimeError(result.stderr.strip() or "tmux 명령 실패")
        return result

    def remote(self, script, tty=False):
        command = ["ssh", "-S", self.config["ssh_socket"], "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=5"]
        command.append("-tt" if tty else "-T")
        command += [self.config["robot"], shlex.join(["/bin/bash", "-c", script])]
        return command

    def execute(self, host, script, timeout=15):
        bounded = ["timeout", "--kill-after=2", str(max(1, timeout - 2)), "/bin/bash", "-c", script]
        command = self.remote("exec " + shlex.join(bounded)) if host == "pi" else bounded
        try:
            return self.run(command, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(command, 124, "", "준비 확인 시간 초과")

    def save(self):
        path = self.directory / "state.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)

    def alive(self, name):
        pane = self.panes.get(name)
        if pane is None:
            return False
        result = self.tmux("display-message", "-p", "-t", pane, "#{pane_dead}", check=False)
        return result.returncode == 0 and result.stdout.strip() == "0"

    def start_job(self, job):
        gate = self.directory / (job.name + ".go")
        command = shlex.join(self.remote(job.command, tty=True)) if job.host == "pi" else job.command
        wrapper = ("while [[ ! -f " + quote(gate) + " ]]; do sleep 0.1; done\n"
                   + "exec /bin/bash -c " + quote(command))
        result = self.tmux("new-window", "-d", "-P", "-F", "#{pane_id}",
                           "-t", SESSION + ":" + str(job.window), "-n", job.name,
                           "/bin/bash", "-c", wrapper)
        pane = result.stdout.strip()
        if not pane.startswith("%") or not pane[1:].isdigit():
            raise RuntimeError("tmux 창 식별자를 받지 못했습니다.")
        self.panes[job.name] = pane
        self.save()
        log_command = "cat >> " + quote(self.directory / (job.name + ".log"))
        self.tmux("pipe-pane", "-o", "-t", pane, log_command)
        gate.touch()

    def wait_ready(self, job):
        deadline = self.clock() + self.config["timeout"]
        last_error = ""
        while self.clock() < deadline:
            dead = [name for name in self.panes if not self.alive(name)]
            if dead:
                raise RuntimeError("프로그램이 종료됐습니다: " + ", ".join(dead))
            result = self.execute("pi" if job.name == "voice" else "pc", job.probe)
            last_error = (result.stderr or result.stdout)[-1000:]
            if result.returncode == 0:
                if job.name == "bridge":
                    log = self.directory / "bridge.log"
                    text = log.read_text(errors="replace") if log.exists() else ""
                    connected = text.rfind("Serial connected;")
                    failure = max(text.rfind("Serial connection lost:"), text.rfind("Unable to open "))
                    if connected < 0 or failure > connected:
                        self.sleep(0.5)
                        continue
                    self.sleep(3)  # Existing bridge waits 2.5 seconds after serial open.
                    text = log.read_text(errors="replace")
                    if max(text.rfind("Serial connection lost:"), text.rfind("Unable to open ")) > text.rfind("Serial connected;"):
                        continue
                return
            self.sleep(0.5)
        raise RuntimeError(job.name + " 준비 시간 초과\n" + last_error)

    def start(self):
        self.tmux("new-session", "-d", "-s", SESSION, "-n", "overview", "/bin/bash")
        self.tmux("set-option", "-g", "remain-on-exit", "on")
        self.tmux("set-option", "-g", "automatic-rename", "off")
        # tmux may have been left running after an earlier session. Refresh GUI variables.
        for key in ("DISPLAY", "XAUTHORITY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR"):
            if key in os.environ:
                self.tmux("set-environment", "-g", key, os.environ[key])
            else:
                self.tmux("set-environment", "-gu", key)
        self.save()
        for number, job in enumerate(self.jobs, 1):
            print(f"[{number}/10] {job.host} {job.name} 시작·준비 확인", flush=True)
            self.start_job(job)
            self.wait_ready(job)
        self.tmux("select-window", "-t", SESSION + ":1")
        print("준비 완료. RViz 초기 위치·순찰 지점 지정 후 웹에서 주행 시작을 누르세요.", flush=True)

    def stop_job(self, name):
        if not self.alive(name):
            return True
        self.tmux("send-keys", "-t", self.panes[name], "C-c")
        deadline = self.clock() + 15
        while self.clock() < deadline:
            if not self.alive(name):
                return True
            self.sleep(0.2)
        return False

    def stop(self):
        if self.alive("server"):
            self.execute("pc", "curl -fsS --max-time 3 -X PUT http://127.0.0.1:8000/control "
                         + "-H 'Content-Type: application/json' --data '{\"drive\":\"stop\"}'")
        if not self.stop_job("patrol"):
            raise RuntimeError("순찰이 종료되지 않았습니다. patrol 창에서 확인하세요. 나머지 통신은 유지합니다.")
        failed = []
        for name in ("voice", "outputs", "bridge", "detector", "localization", "video", "camera", "bringup", "server"):
            if name in self.panes:
                print(name + " 종료 확인", flush=True)
            if not self.stop_job(name):
                if name == "outputs":
                    raise RuntimeError("출력 중계가 종료되지 않았습니다. outputs 창에서 확인한 뒤 stop을 다시 실행하세요. 브리지·센서·서버와 SSH 연결은 유지합니다.")
                failed.append(name)
            if name == "outputs" and name in self.panes:
                self.sleep(1)  # Allow the existing OFF publications to reach the Pi bridge.
        if failed:
            raise RuntimeError("종료를 확인하지 못했습니다: " + ", ".join(failed))
        self.tmux("kill-session", "-t", SESSION, check=False)
        self.run(["ssh", "-S", self.config["ssh_socket"], "-O", "exit", self.config["robot"]],
                 capture_output=True, text=True, timeout=10)
        self.config["stopped"] = True
        self.save()
        print("종료 완료. 기능별 로그는 " + str(self.directory) + "에 있습니다.")


def preflight(launcher):
    config = launcher.config
    if launcher.tmux("has-session", "-t", SESSION, check=False).returncode == 0:
        raise RuntimeError("기존 demo 세션이 있습니다. status 또는 attach로 확인하세요.")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        raise RuntimeError("RViz를 표시할 서버 PC의 데스크톱 터미널에서 실행하세요.")
    pc = environment(config, "pc")
    tests = ["test -x backend/.venv/bin/python", "test -r src/maps/new_map.yaml",
             "test -r src/maps/new_map.pgm", "test -x " + shell_path(config["whisper_cli"]),
             "test -r " + shell_path(config["model"]),
             "ros2 pkg prefix my_bot_fall >/dev/null", "ros2 pkg prefix nav2_bringup >/dev/null",
             'test -r "$(ros2 pkg prefix my_bot_fall)/share/my_bot_fall/launch/tb3_localization_real.launch.py"',
             'test -r "$(ros2 pkg prefix my_bot_fall)/share/my_bot_fall/models/fall_pose_n.pt"',
             "ros2 pkg prefix web_video_server >/dev/null", "ros2 pkg prefix rviz2 >/dev/null",
             'nodes=$(ros2 node list)\nif grep -Eq "^/(astar_driver_yolo|lying_detector|fall_patrol_voice|tb3_arduino_bridge|amcl|map_server|rviz2|v4l2_camera|turtlebot3_node)$" <<< "$nodes"; then echo "기존 실행 노드를 종료한 뒤 시작하세요." >&2; exit 1; fi',
             NO_EXISTING_DRIVER,
             "/usr/bin/python3 -c " + quote("import socket; sockets=[]\nfor port in (8000,8080):\n s=socket.socket(); s.bind(('0.0.0.0',port)); sockets.append(s)")]
    checks = []
    for test in tests:
        if "\n" in test:
            checks.append(test)
        else:
            checks.append(test + " || { echo " + quote("필요 조건 확인 실패: " + test) + " >&2; exit 1; }")
    result = launcher.execute("pc", pc + "\n".join(checks), timeout=30)
    if result.returncode:
        raise RuntimeError("PC 준비 확인 실패\n" + result.stderr)
    pi = environment(config, "pi")
    pi += "for tool in curl mpg123 arecord amixer timeout; do command -v \"$tool\" >/dev/null || exit 1; done\n"
    pi += 'nodes=$(ros2 node list)\nif grep -Eq "^/(fall_patrol_voice|tb3_arduino_bridge|v4l2_camera|turtlebot3_node)$" <<< "$nodes"; then echo "Pi의 기존 실행 노드를 종료하세요." >&2; exit 1; fi\n'
    pi += "ros2 pkg prefix my_bot_fall >/dev/null\nros2 pkg prefix tb3_arduino_io >/dev/null\n"
    pi += ('for file in voice/stt/whisper_session.py voice/stt/response_classifier.py voice/tts/observe.mp3 voice/tts/alert.mp3; do\n'
           '  [[ -r "$file" ]] || { echo "필요한 Pi 파일이 없습니다: $file" >&2; exit 1; }\ndone\n')
    pi += device_command(config["opencr"], "/dev/serial/by-id/usb-ROBOTIS_OpenCR*", "OpenCR")
    pi += 'opencr_device=$(readlink -f "$device")\n'
    pi += device_command(config["mcu"], "/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink*", "NUCLEO")
    pi += '[[ "$opencr_device" != "$(readlink -f "$device")" ]]\n'
    pi += "amixer -c " + quote(config["speaker_card"]) + " sget PCM >/dev/null\n"
    pi += environment(config, "pi", "robot")
    pi += "ros2 pkg prefix turtlebot3_bringup >/dev/null\nros2 pkg prefix v4l2_camera >/dev/null\n"
    result = launcher.execute("pi", pi, timeout=30)
    if result.returncode:
        raise RuntimeError("Pi 준비 확인 실패\n" + result.stderr)


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status", "attach", "plan"))
    parser.add_argument("--robot", help="Pi SSH 접속 대상, 예: ubuntu@ROBOT_IP")
    parser.add_argument("--server-url", help="Pi에서 접근할 서버 주소, 예: http://SERVER_IP:8000")
    parser.add_argument("--pi-root", default="~/fall_patrol/project_ws")
    parser.add_argument("--domain", default=os.environ.get("ROS_DOMAIN_ID", "33"))
    parser.add_argument("--opencr-port", default="")
    parser.add_argument("--mcu-port", default="")
    parser.add_argument("--input-device", default="plughw:CARD=microphone,DEV=0")
    parser.add_argument("--output-device", default="plughw:CARD=UACDemoV10,DEV=0")
    parser.add_argument("--speaker-card", default="UACDemoV10")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--no-attach", action="store_true")
    args = parser.parse_args(argv)
    if args.action in ("start", "plan"):
        if not args.robot or args.robot.startswith("-") or any(c.isspace() for c in args.robot):
            parser.error("--robot에 Pi의 SSH 접속 대상을 지정하세요.")
        url = urlparse(args.server_url or "")
        if url.scheme != "http" or not url.hostname or url.username or url.password or url.path not in ("", "/") or url.query or url.fragment:
            parser.error("--server-url에 http://SERVER_IP:8000 형태의 서버 주소를 지정하세요.")
        if url.hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            parser.error("Pi에서 접근할 수 있는 서버 PC 주소를 지정하세요.")
        if url.port != 8000:
            parser.error("기존 FastAPI 실행 포트인 8000을 지정하세요.")
        if not args.domain.isdigit() or not 0 <= int(args.domain) <= 232 or args.timeout < 1:
            parser.error("ROS 도메인은 0~232, 준비 제한 시간은 양수여야 합니다.")
    return args


def main(argv=None):
    args = arguments(argv)
    state_root = ROOT / "log" / "demo"
    if args.action in ("start", "plan"):
        run_dir = state_root / (time.strftime("%Y%m%d-%H%M%S") + "-" + str(os.getpid()))
        config = dict(pc_root=str(ROOT), pi_root=args.pi_root, robot=args.robot,
                      server_url=args.server_url.rstrip("/"), domain=args.domain,
                      rmw=os.environ.get("RMW_IMPLEMENTATION", ""), opencr=args.opencr_port,
                      mcu=args.mcu_port, input_device=args.input_device, output_device=args.output_device,
                      speaker_card=args.speaker_card, timeout=args.timeout, run_dir=str(run_dir),
                      whisper_cli=os.environ.get("FALL_WHISPER_CLI", "~/whisper.cpp/build/bin/whisper-cli"),
                      model=os.environ.get("FALL_WHISPER_MODEL", "~/whisper.cpp/models/ggml-base-q8_0.bin"))
        if args.action == "plan":
            for number, job in enumerate(build_jobs(config), 1):
                print(f"[{number}/10] {job.host} {job.name}\n{job.command}\n")
            return 0
    for name in ("tmux", "ssh", "curl", "timeout"):
        if not shutil.which(name):
            raise RuntimeError(name + " 명령이 필요합니다.")
    state_root.mkdir(parents=True, exist_ok=True)
    with (state_root / "launcher.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RuntimeError("다른 시작·종료 작업이 진행 중입니다.") from error
        current = state_root / "current"
        if args.action == "start":
            if current.exists():
                previous = json.loads((Path(current.read_text().strip()) / "state.json").read_text())
                if not previous.get("stopped"):
                    raise RuntimeError("이전 실행을 stop으로 정리한 뒤 시작하세요.")
            config["ssh_socket"] = tempfile.mkdtemp(prefix="tw-ssh-") + "/control"
            launcher = Launcher(config)
            # Authenticate once, through the user's terminal. Later windows reuse this connection.
            subprocess.run(["ssh", "-MNf", "-S", config["ssh_socket"], "-o", "ControlMaster=yes",
                            "-o", "ControlPersist=600", "-o", "ConnectTimeout=10",
                            "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=3", config["robot"]], check=True)
            try:
                preflight(launcher)
            except BaseException:
                subprocess.run(["ssh", "-S", config["ssh_socket"], "-O", "exit", config["robot"]],
                               capture_output=True, timeout=10)
                raise
            run_dir.mkdir(mode=0o700)
            launcher.save()
            current.write_text(str(run_dir) + "\n")
            try:
                launcher.start()
            except BaseException:
                print("시작을 중단했습니다. 실행된 프로그램을 종료합니다.", file=sys.stderr)
                launcher.stop()
                raise
        else:
            if not current.exists():
                raise RuntimeError("이 스크립트로 시작한 실행 기록이 없습니다.")
            config = json.loads((Path(current.read_text().strip()) / "state.json").read_text())
            launcher = Launcher(config)
            if args.action == "stop":
                if not config.get("stopped"):
                    launcher.stop()
                return 0
            if args.action == "status":
                for job in launcher.jobs:
                    print(job.name + " " + ("running" if launcher.alive(job.name) else "stopped"))
                print("로그 " + str(launcher.directory))
                return 0
        if not args.no_attach:
            fcntl.flock(lock, fcntl.LOCK_UN)
            os.execvp("tmux", ["tmux", "-L", SOCKET, "attach-session", "-t", SESSION])
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
