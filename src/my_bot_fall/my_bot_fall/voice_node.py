"""Pi-only ROS adapter for the existing voice files; no HTTP/event creation."""

import json
import os
from pathlib import Path
import queue
import signal
import subprocess
import sys
import threading
import time

from .voice_link import valid_request


def stop_process(process):
    if process.poll() is not None:
        process.communicate()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.communicate(timeout=2.0)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.communicate()


def run_process(command, request, cancel):
    process = None
    try:
        remaining = request["expires_at"] - time.time()
        if remaining <= 0:
            raise TimeoutError("voice request expired")
        deadline = time.monotonic() + remaining
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)
        while True:
            if cancel.is_set():
                raise RuntimeError("voice request cancelled")
            if time.monotonic() >= deadline:
                raise TimeoutError("voice process timed out")
            try:
                stdout, stderr = process.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        try:
            result = json.loads(stdout)
        except (TypeError, ValueError) as error:
            raise ValueError("voice process did not return JSON: " + stderr[-500:]) from error
        if not isinstance(result, dict) or result.get("status") not in ("ok", "error"):
            raise ValueError("invalid voice result")
        if process.returncode and result["status"] != "error":
            raise RuntimeError("voice process failed: " + stderr[-500:])
        return result
    except Exception as error:
        return {"status": "error", "stage": "worker", "message": str(error)[:1000]}
    finally:
        if process is not None:
            stop_process(process)


class VoiceWorker:
    """One exchange at a time; repeat requests reuse the completed result."""

    def __init__(self, command, runner=run_process, state_path=None):
        self.command = command
        self.runner = runner
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None
        self.thread = None
        self.closed = False
        self.results = queue.SimpleQueue()
        self.state_path = Path(state_path).expanduser() if state_path else None
        if self.state_path and self.state_path.exists():
            saved = json.loads(self.state_path.read_text())
            if not isinstance(saved, dict):
                raise ValueError('invalid voice request journal')
            for key, entry in saved.items():
                expires = float(entry['expires_at'])
                if expires + 5 <= time.time():
                    continue
                cancel = threading.Event()
                if entry.get('cancelled'):
                    cancel.set()
                result = entry.get('result') or {
                    'request_id': key, 'status': 'error', 'stage': 'restart',
                    'message': 'Pi 음성 노드가 재시작되어 같은 요청을 다시 재생하지 않았습니다.'}
                self.jobs[key] = dict(expires_at=expires, cancel=cancel, result=result)

    def _save(self):
        if self.state_path is None:
            return
        saved = {key: dict(expires_at=job['expires_at'], cancelled=job['cancel'].is_set(),
                           result=job['result']) for key, job in self.jobs.items()}
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(saved, ensure_ascii=False))
        temporary.replace(self.state_path)

    def receive(self, request):
        now = time.time()
        if not valid_request(request, now):
            return
        key = request["request_id"]
        with self.lock:
            if self.closed:
                return
            self.jobs = {k: v for k, v in self.jobs.items()
                         if v["expires_at"] + 5 > now or k == self.active}
            if request["action"] == "cancel":
                job = self.jobs.setdefault(key, {"expires_at": request["expires_at"],
                                                "cancel": threading.Event(), "result": None})
                job["cancel"].set()
                self._save()
                return
            if key in self.jobs:
                job = self.jobs[key]
                if job["result"] is not None and not job["cancel"].is_set():
                    self.results.put(job["result"])
                return
            # A request for another observation is retried by the controller;
            # do not queue old audio or overlap microphone/Whisper processes.
            if self.active is not None:
                return
            job = {"expires_at": request["expires_at"], "cancel": threading.Event(), "result": None}
            self.jobs[key] = job
            try:
                # Persist identity before playing. An interrupted session is
                # reported as an error after restart rather than replayed.
                self._save()
            except OSError as error:
                job['result'] = dict(request_id=key, status='error', stage='journal', message=str(error))
                self.results.put(job['result'])
                return
            self.active = key
            self.thread = threading.Thread(target=self._run, args=(dict(request), job), daemon=True)
            self.thread.start()

    def _run(self, request, job):
        key = request["request_id"]
        try:
            result = dict(self.runner(self.command, request, job["cancel"]), request_id=key)
        except Exception as error:
            result = {"request_id": key, "status": "error", "stage": "worker", "message": str(error)}
        with self.lock:
            job["result"] = result
            self.active = None
            try:
                self._save()
            except OSError as error:
                result = dict(request_id=key, status='error', stage='journal', message=str(error))
            if not self.closed and not job["cancel"].is_set():
                self.results.put(result)

    def close(self):
        with self.lock:
            self.closed = True
            for job in self.jobs.values():
                job["cancel"].set()
            thread = self.thread
        if thread is not None:
            thread.join(timeout=4.0)


def main(args=None):
    # Import ROS only in the adapter: the worker can be tested without ROS.
    import rclpy
    from rclpy.node import Node
    from rclpy.signals import SignalHandlerOptions
    from std_msgs.msg import String

    rclpy.init(args=args, signal_handler_options=SignalHandlerOptions.NO)
    node = Node("fall_patrol_voice")
    worker = None
    try:
        p = lambda name, default: node.declare_parameter(name, default).value
        script = Path(p("script_path", "")).expanduser().resolve()
        if not script.is_file():
            raise FileNotFoundError("script_path에 whisper_session.py의 실제 경로를 지정하세요.")
        command = [p("python_executable", sys.executable), str(script),
                   "--whisper-cli", str(Path(p("whisper_cli", "~/whisper.cpp/build/bin/whisper-cli")).expanduser()),
                   "--model", str(Path(p("model_path", "~/whisper.cpp/models/ggml-base-q8_0.bin")).expanduser()),
                   "--prompt", str(Path(p("prompt_path", str(script.parents[1] / "tts/observe.mp3"))).expanduser()),
                   "--input-device", p("input_device", "default"),
                   "--output-device", p("output_device", "")]
        worker = VoiceWorker(command, state_path=p('state_file', '~/.turtlewatch/voice_requests.json'))
        publisher = node.create_publisher(String, "/fall_patrol/voice_result", 10)

        def receive(message):
            try:
                if len(message.data) <= 16000:
                    worker.receive(json.loads(message.data))
            except (TypeError, ValueError):
                node.get_logger().warn("잘못된 음성 요청 JSON을 무시했습니다.")

        def publish_results():
            while True:
                try:
                    result = worker.results.get_nowait()
                except queue.Empty:
                    return
                publisher.publish(String(data=json.dumps(result, ensure_ascii=False)))

        node.create_subscription(String, "/fall_patrol/voice_request", receive, 10)
        node.create_timer(0.1, publish_results)
        node.get_logger().info("Pi 음성 요청 대기: 녹음은 임시 파일만 사용하고 서버 이벤트는 생성하지 않습니다.")
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if worker is not None:
            worker.close()
        node.destroy_node()
        rclpy.try_shutdown()


if __name__ == "__main__":
    main()
