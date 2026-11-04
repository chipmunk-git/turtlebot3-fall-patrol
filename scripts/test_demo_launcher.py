"""Launcher regression tests. No SSH, tmux server, ROS2, audio, or robot is started."""

import importlib.util
import io
import json
from pathlib import Path
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("demo_launcher", Path(__file__).with_name("demo_launcher.py"))
DEMO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEMO
SPEC.loader.exec_module(DEMO)


def setUpModule():
    # Tests must never write to a dashboard that happens to be running on this PC.
    patcher = patch.object(DEMO, "post_log", lambda *a, **k: True)
    patcher.start()
    unittest.addModuleCleanup(patcher.stop)


class DashboardLogTests(unittest.TestCase):
    def test_failure_is_reported_only_for_start_and_patrol_restart(self):
        with patch.object(DEMO, "post_log") as post:
            DEMO.report_failure(["start", "--robot", "u@h"], RuntimeError("camera 준비 시간 초과\n"))
            DEMO.report_failure(["patrol-restart"], RuntimeError("종료됨"))
            DEMO.report_failure(["status"], RuntimeError("무시"))
        self.assertEqual([c.args for c in post.call_args_list], [
            ("error", "로봇 프로그램 시작 실패: camera 준비 시간 초과"), ("error", "순찰 재시작 실패: 종료됨")])

    def test_exit_hook_is_off_during_requested_exits(self):
        config = dict(pc_root="/w", pi_root="~/p", robot="u@h", server_url="http://s:8000", domain="33", rmw="",
                      opencr="", mcu="", speaker_card="x", input_device="x", output_device="x", timeout=2,
                      run_dir=tempfile.mkdtemp(), ssh_socket="/s", whisper_cli="~/a", model="~/b", external_server=True)
        self.addCleanup(shutil.rmtree, config["run_dir"])
        order = []
        launcher = DEMO.Launcher(config, run=lambda *a, **k: subprocess.CompletedProcess([], 0), sleep=lambda _: None)
        launcher.panes["patrol"] = "%1"
        launcher.send_web_stop = lambda: None
        launcher.wait_ready = lambda job: None
        launcher.stop_job = lambda name: order.append("stop") or True
        launcher.tmux = lambda *a, **k: order.append(" ".join(a[:3])) or subprocess.CompletedProcess([], 0, "", "")
        with patch("sys.stdout", io.StringIO()):
            launcher.restart_patrol()
        self.assertEqual(order[:3], ["set-hook -gu pane-died", "stop", "set-hook -g pane-died"])


class ExternalServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = dict(
            pc_root="/workspace with spaces/project_ws", pi_root="~/fall_patrol/project_ws",
            robot="robot@example.invalid", server_url="http://server.invalid:8000",
            domain="33", rmw="", opencr="", mcu="", speaker_card="UACDemoV10",
            input_device="plughw:CARD=microphone,DEV=0", output_device="plughw:CARD=UACDemoV10,DEV=0",
            timeout=2, run_dir=self.temp.name, ssh_socket=self.temp.name + "/ssh",
            whisper_cli="~/whisper.cpp/build/bin/whisper-cli",
            model="~/whisper.cpp/models/ggml-base-q8_0.bin", external_server=True,
        )

    @staticmethod
    def done(code=0, stderr=""):
        return subprocess.CompletedProcess([], code, "", stderr)

    def test_robot_programs_keep_their_order_without_the_server_job(self):
        self.assertEqual([j.name for j in DEMO.build_jobs(self.config)], [
            "bringup", "camera", "bridge", "voice", "outputs",
            "localization", "video", "detector", "patrol"])

    def test_server_command_is_identical_in_both_modes(self):
        default = dict(self.config, external_server=False)
        job = next(j for j in DEMO.build_jobs(default) if j.name == "server")
        self.assertEqual(job.command, DEMO.server_command(self.config))
        for text in ("--workers 1", "FALL_WHISPER_CLI", "FALL_WHISPER_MODEL", "--host 0.0.0.0"):
            self.assertIn(text, job.command)

    def test_stop_still_sends_web_stop_and_leaves_the_server_running(self):
        events = []
        launcher = DEMO.Launcher(self.config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.panes.update({j.name: "%" + str(j.window) for j in launcher.jobs})
        launcher.alive = lambda name: name in launcher.panes
        launcher.execute = lambda host, script, **k: events.append(("web-stop", script)) or self.done()
        launcher.stop_job = lambda name: events.append(name) or True
        launcher.tmux = lambda *a, **k: self.done()
        launcher.save = lambda: None
        with patch("sys.stdout", io.StringIO()):
            launcher.stop()
        self.assertEqual(events[0][0], "web-stop")
        self.assertIn('{"drive":"stop"}', events[0][1])
        self.assertEqual(events[1:], ["patrol", "voice", "outputs", "bridge", "detector", "localization", "video", "camera", "bringup"])

    def test_stop_continues_and_warns_when_web_stop_fails(self):
        events = []
        launcher = DEMO.Launcher(self.config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.alive = lambda name: False
        launcher.execute = lambda *a, **k: self.done(7)
        launcher.stop_job = lambda name: events.append(name) or True
        launcher.tmux = lambda *a, **k: self.done()
        launcher.save = lambda: None
        error = io.StringIO()
        with patch("sys.stdout", io.StringIO()), patch("sys.stderr", error):
            launcher.stop()
        self.assertIn("주행 정지 명령을 보내지 못했습니다", error.getvalue())
        self.assertEqual(events[0], "patrol")

    def test_default_mode_still_skips_web_stop_when_its_server_is_gone(self):
        config = dict(self.config, external_server=False)
        calls = []
        launcher = DEMO.Launcher(config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.alive = lambda name: False
        launcher.execute = lambda *a, **k: calls.append(a) or self.done()
        launcher.tmux = lambda *a, **k: self.done()
        launcher.save = lambda: None
        with patch("sys.stdout", io.StringIO()):
            launcher.stop()
        self.assertEqual(calls, [])

    def test_preflight_requires_this_workspace_server_and_keeps_video_port_check(self):
        scripts = []
        launcher = DEMO.Launcher(self.config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.tmux = lambda *a, **k: self.done(1)
        launcher.execute = lambda host, script, **k: scripts.append(script) or self.done()
        with patch.dict("os.environ", {"DISPLAY": ":0"}):
            DEMO.preflight(launcher)
        self.assertIn("/workspace with spaces/project_ws/backend", scripts[0])
        self.assertIn("for port in (8080,)", scripts[0])

    def test_default_preflight_keeps_both_ports_and_hints_external_mode(self):
        config = dict(self.config, external_server=False)
        launcher = DEMO.Launcher(config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.tmux = lambda *a, **k: self.done(1)
        scripts = []
        launcher.execute = lambda host, script, **k: scripts.append(script) or self.done(1, "필요 조건 확인 실패: import socket")
        with patch.dict("os.environ", {"DISPLAY": ":0"}), self.assertRaisesRegex(RuntimeError, "--external-server"):
            DEMO.preflight(launcher)
        self.assertIn("for port in (8000, 8080)", scripts[0])

    def test_wait_ready_stops_when_external_server_disappears(self):
        launcher = DEMO.Launcher(self.config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.execute = lambda *a, **k: self.done(1, "서버 응답 없음")
        with self.assertRaisesRegex(RuntimeError, "외부 서버 확인 실패"):
            launcher.wait_ready(launcher.jobs[0])

    def test_server_check_rejects_a_server_from_another_workspace(self):
        directory = Path(self.temp.name)
        for name, body in (("curl", "echo '{\"status\":\"ok\"}'"),
                           ("ss", "echo 'pid=1'"),
                           ("readlink", "echo /other/workspace/backend")):
            path = directory / name
            path.write_text("#!/bin/bash\n" + body + "\n")
            path.chmod(0o755)
        env = {"PATH": str(directory) + ":/usr/bin:/bin"}
        script = DEMO.server_check(self.config)
        wrong = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(wrong.returncode, 1)
        self.assertIn("/other/workspace/backend", wrong.stderr)
        (directory / "readlink").write_text("#!/bin/bash\necho '/workspace with spaces/project_ws/backend'\n")
        right = subprocess.run(["/bin/bash", "-c", script], env=env, capture_output=True, text=True)
        self.assertEqual(right.returncode, 0, right.stderr)

    def manager(self, healthy=False, port_used=False, session=False, owner=""):
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            if command[0] == "tmux":
                return self.done(0 if session or command[5] != "has-session" else 1)
            script = command[-1]
            if "socket.socket" in script:
                return self.done(1 if port_used else 0)
            if 'printf "%s"' in script:
                return subprocess.CompletedProcess([], 0, owner, "")
            return self.done(0 if healthy else 1)
        return DEMO.ServerManager(self.config, Path(self.temp.name) / "server.log", run=run, sleep=lambda _: None), calls

    def test_server_start_reuses_this_workspace_server(self):
        manager, calls = self.manager(healthy=True, port_used=True)
        with patch("sys.stdout", io.StringIO()):
            manager.start()
        self.assertFalse([c for c in calls if "new-session" in c])

    def test_server_start_refuses_a_port_used_by_another_workspace(self):
        manager, calls = self.manager(port_used=True, owner="/other/workspace/backend")
        with self.assertRaisesRegex(RuntimeError, "/other/workspace/backend"):
            manager.start()
        self.assertFalse([c for c in calls if "new-session" in c])

    def test_server_stop_never_touches_a_server_it_did_not_start(self):
        manager, calls = self.manager(port_used=True, owner="/other/workspace/backend")
        with self.assertRaisesRegex(RuntimeError, "server-start로 시작한 서버가 아닙니다"):
            manager.stop()
        self.assertFalse([c for c in calls if "send-keys" in c])

    def test_server_logs_are_connected_before_program_is_released(self):
        manager, calls = self.manager()
        health = iter((False, True))
        manager.healthy = lambda: next(health)
        gate = manager.log_path.with_suffix(".go")
        gate.touch()  # A previous run must not release the new server early.
        original = manager.tmux

        def tmux(*args, **kwargs):
            if args[0] in ("new-session", "pipe-pane"):
                self.assertFalse(gate.exists())
            return original(*args, **kwargs)

        manager.tmux = tmux
        with patch("sys.stdout", io.StringIO()):
            manager.start()
        self.assertTrue(gate.exists())
        commands = [c[5] for c in calls if c[0] == "tmux"]
        self.assertLess(commands.index("new-session"), commands.index("pipe-pane"))

    def test_server_logging_failure_does_not_release_the_program(self):
        manager, _ = self.manager()
        commands = []

        def tmux(*args, **kwargs):
            commands.append(args[0])
            if args[0] == "has-session":
                return self.done(1)
            if args[0] == "pipe-pane":
                raise RuntimeError("log unavailable")
            return self.done()

        manager.tmux = tmux
        with self.assertRaisesRegex(RuntimeError, "log unavailable"):
            manager.start()
        self.assertFalse(manager.log_path.with_suffix(".go").exists())
        self.assertEqual(commands[-1], "kill-session")

    def test_port_check_detects_listener_but_ignores_time_wait(self):
        listener = socket.socket()
        self.addCleanup(listener.close)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        client = socket.create_connection(("127.0.0.1", port))
        accepted, _ = listener.accept()
        accepted.close()  # The closing side holds this port in TIME_WAIT.
        client.close()
        manager = DEMO.ServerManager(self.config, Path(self.temp.name) / "server.log")
        with patch.object(DEMO, "SERVER_PORT", port):
            # A loopback listener must count even when wildcard binding is allowed.
            self.assertTrue(manager.port_in_use())
            listener.close()
            self.assertFalse(manager.port_in_use())

    def restart_launcher(self, stopped=True):
        events = []
        launcher = DEMO.Launcher(self.config, run=lambda *a, **k: self.done(), sleep=lambda _: None)
        launcher.panes["patrol"] = "%1"
        launcher.send_web_stop = lambda: events.append("web-stop")
        launcher.stop_job = lambda name: events.append("stop:" + name) or stopped
        launcher.tmux = lambda *a, **k: events.append(a[0]) or self.done()
        launcher.wait_ready = lambda job: events.append("ready:" + job.name)
        return launcher, events

    def test_patrol_restart_stops_first_and_touches_only_patrol(self):
        launcher, events = self.restart_launcher()
        with patch("sys.stdout", io.StringIO()):
            launcher.restart_patrol()
        self.assertEqual([e for e in events if e != "set-hook"],
                         ["web-stop", "stop:patrol", "respawn-pane", "pipe-pane", "ready:patrol"])

    def test_patrol_restart_does_not_respawn_when_patrol_will_not_stop(self):
        launcher, events = self.restart_launcher(stopped=False)
        with self.assertRaisesRegex(RuntimeError, "순찰이 종료되지 않았습니다"):
            launcher.restart_patrol()
        self.assertNotIn("respawn-pane", events)

    def test_patrol_restart_reconnects_logs_before_releasing_the_program(self):
        launcher, _ = self.restart_launcher()
        gate = launcher.directory / "patrol.go"
        gate.touch()

        def tmux(*args, **kwargs):
            if args[0] in ("respawn-pane", "pipe-pane"):
                self.assertFalse(gate.exists())
            return self.done()

        launcher.tmux = tmux
        with patch("sys.stdout", io.StringIO()):
            launcher.restart_patrol()
        self.assertTrue(gate.exists())

    def test_patrol_restart_logging_failure_keeps_the_program_waiting(self):
        launcher, _ = self.restart_launcher()
        gate = launcher.directory / "patrol.go"
        gate.touch()

        def tmux(*args, **kwargs):
            if args[0] == "pipe-pane":
                raise RuntimeError("log unavailable")
            return self.done()

        launcher.tmux = tmux
        with self.assertRaisesRegex(RuntimeError, "log unavailable"):
            launcher.restart_patrol()
        self.assertFalse(gate.exists())

    def test_exit_hook_failure_does_not_block_patrol_restart(self):
        launcher, _ = self.restart_launcher()
        commands = []

        def run(command, **kwargs):
            commands.append(command[5])
            return self.done(1 if command[5] == "set-hook" else 0)

        launcher.run = run
        launcher.tmux = DEMO.Launcher.tmux.__get__(launcher)
        with patch("sys.stdout", io.StringIO()):
            launcher.restart_patrol()
        self.assertIn("respawn-pane", commands)
        self.assertIn("pipe-pane", commands)

    def test_unexpected_stop_error_restores_exit_monitoring(self):
        launcher, _ = self.restart_launcher()
        commands = []
        launcher.unwatch_programs = lambda: commands.append("unwatch")
        launcher.watch_programs = lambda: commands.append("watch")

        def stop_programs():
            raise subprocess.TimeoutExpired("tmux", 10)

        launcher.stop_programs = stop_programs
        with self.assertRaises(subprocess.TimeoutExpired):
            launcher.stop()
        self.assertEqual(commands, ["unwatch", "watch"])

    def run_main_until_ssh(self, argv):
        calls = []
        class Manager:
            def __init__(self, *a, **k): pass
            def start(self): calls.append("server-start")
        def ssh(*a, **k):
            calls.append("ssh")
            raise subprocess.CalledProcessError(255, "ssh")
        with patch.object(DEMO, "ROOT", Path(self.temp.name)), patch.object(DEMO.shutil, "which", return_value="/mock/tool"), \
                patch.object(DEMO, "ServerManager", Manager), \
                patch.object(DEMO.subprocess, "run", ssh), self.assertRaises(subprocess.SubprocessError):
            DEMO.main(argv)
        return calls

    def test_external_start_brings_up_the_server_before_the_robot(self):
        base = ["start", "--robot", "u@h", "--server-url", "http://1.2.3.4:8000"]
        self.assertEqual(self.run_main_until_ssh(base + ["--external-server"]), ["server-start", "ssh"])
        self.assertEqual(self.run_main_until_ssh(base), ["ssh"])

    def test_patrol_restart_requires_a_running_demo(self):
        root = Path(self.temp.name)
        run_dir = root / "run"
        run_dir.mkdir()
        (run_dir / "state.json").write_text(json.dumps(dict(self.config, stopped=True, run_dir=str(run_dir))))
        state_root = root / "log" / "demo"
        state_root.mkdir(parents=True)
        (state_root / "current").write_text(str(run_dir) + "\n")
        with patch.object(DEMO, "ROOT", root), patch.object(DEMO.shutil, "which", return_value="/mock/tool"), \
                self.assertRaisesRegex(RuntimeError, "start를 먼저"):
            DEMO.main(["patrol-restart"])

    def test_demo_and_server_use_separate_tmux_servers(self):
        self.assertNotEqual(DEMO.SOCKET, DEMO.SERVER_SOCKET)

    def test_external_flag_and_server_actions_parse(self):
        args = DEMO.arguments(["start", "--robot", "u@h", "--server-url", "http://1.2.3.4:8000", "--external-server"])
        self.assertTrue(args.external_server)
        self.assertFalse(DEMO.arguments(["start", "--robot", "u@h", "--server-url", "http://1.2.3.4:8000"]).external_server)
        for action in ("server-start", "server-stop", "server-status", "patrol-restart"):
            self.assertEqual(DEMO.arguments([action]).action, action)


class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = dict(
            pc_root="/workspace with spaces/project_ws", pi_root="~/fall_patrol/project_ws",
            robot="robot@example.invalid", server_url="http://server.invalid:8000",
            domain="33", rmw="", opencr="", mcu="", speaker_card="UACDemoV10",
            input_device="plughw:CARD=microphone,DEV=0", output_device="plughw:CARD=UACDemoV10,DEV=0",
            timeout=2, run_dir=self.temp.name, ssh_socket=self.temp.name + "/ssh",
            whisper_cli="~/whisper.cpp/build/bin/whisper-cli",
            model="~/whisper.cpp/models/ggml-base-q8_0.bin",
        )
        self.jobs = DEMO.build_jobs(self.config)

    def test_handover_order_hosts_and_log_priority(self):
        self.assertEqual([j.name for j in self.jobs], [
            "bringup", "camera", "server", "bridge", "voice", "outputs",
            "localization", "video", "detector", "patrol"])
        self.assertEqual([j.host for j in self.jobs], ["pi", "pi", "pc", "pi", "pi", "pc", "pc", "pc", "pc", "pc"])
        priority = [j.name for j in sorted(self.jobs, key=lambda j: j.window)]
        self.assertEqual(priority[:4], ["patrol", "detector", "voice", "server"])

    def test_original_options_and_server_inference_are_preserved(self):
        jobs = {j.name: j for j in self.jobs}
        self.assertIn('image_raw.jpeg_quality:=50', jobs["camera"].command)
        self.assertIn('time_per_frame:="[1,15]"', jobs["camera"].command)
        self.assertIn('PCM 60%', jobs["voice"].command)
        self.assertIn('-p server_url:=http://server.invalid:8000', jobs["voice"].command)
        self.assertIn('src/maps/new_map.yaml', jobs["localization"].command)
        self.assertIn('-p waypoint_count:=3 -p linear_speed:=0.15', jobs["patrol"].command)
        self.assertIn('--workers 1', jobs["server"].command)

    def test_shell_commands_and_probes_parse_with_spaces(self):
        for job in self.jobs:
            for script in (job.command, job.probe):
                result = subprocess.run(["/bin/bash", "-n"], input=script, text=True, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_device_glob_requires_exactly_one_device_and_supports_override(self):
        automatic = DEMO.device_command("", "/missing/usb-*", "OpenCR")
        self.assertIn('${#devices[@]} == 1', automatic)
        custom = DEMO.device_command("/dev/device with spaces", "ignored", "OpenCR")
        self.assertIn("device='/dev/device with spaces'", custom)
        self.assertNotIn("ignored", custom)

    def test_detector_probe_accepts_ros_yaml_separator_and_rejects_camera_faults(self):
        probe = next(job.probe for job in self.jobs if job.name == "detector")
        code = shlex.split(probe.split("| /usr/bin/python3 -c ", 1)[1])[0]
        for message in ('{"camera_status": "ok"}\n---\n', "'{\"camera_status\": \"ok\"}'\n---\n"):
            with patch("sys.stdin", io.StringIO(message)):
                exec(code, {})
        with patch("sys.stdin", io.StringIO('{"camera_status": "no_frames"}\n---\n')):
            with self.assertRaises(AssertionError):
                exec(code, {})

    def test_remote_arguments_cannot_be_interpreted_as_shell_commands(self):
        launcher = DEMO.Launcher(self.config)
        body = "printf '%s' 'space; $(not-a-command)'"
        command = launcher.remote(body, tty=True)
        self.assertIn("-tt", command)
        self.assertEqual(shlex.split(command[-1]), ["/bin/bash", "-c", body])
        self.assertIn("BatchMode=yes", command)

    def test_logs_are_connected_before_program_is_released(self):
        launcher = DEMO.Launcher(self.config)
        events = []

        def tmux(*args, **kwargs):
            events.append(args[0])
            if args[0] == "pipe-pane":
                self.assertFalse((Path(self.temp.name) / "bringup.go").exists())
            return subprocess.CompletedProcess(args, 0, "%1\n", "")

        launcher.tmux = tmux
        launcher.start_job(self.jobs[0])
        self.assertEqual(events, ["new-window", "pipe-pane"])
        self.assertTrue((Path(self.temp.name) / "bringup.go").exists())
        self.assertEqual(json.loads((Path(self.temp.name) / "state.json").read_text())["panes"], {"bringup": "%1"})

    def test_start_waits_between_each_job_and_never_sends_run(self):
        launcher = DEMO.Launcher(self.config)
        events = []
        launcher.tmux = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        launcher.save = lambda: None
        launcher.start_job = lambda job: events.append(("start", job.name))
        launcher.wait_ready = lambda job: events.append(("ready", job.name))
        launcher.start()
        self.assertEqual(events, [event for job in self.jobs for event in (("start", job.name), ("ready", job.name))])
        all_commands = "\n".join(j.command + j.probe for j in self.jobs)
        self.assertNotIn('"drive":"run"', all_commands)
        self.assertNotIn('/initialpose', all_commands)
        self.assertNotIn('topic pub /goal_pose', all_commands)

    def test_failed_preparation_does_not_start_later_jobs(self):
        launcher = DEMO.Launcher(self.config)
        events = []
        launcher.tmux = lambda *a, **k: subprocess.CompletedProcess(a, 0, "", "")
        launcher.save = lambda: None
        launcher.start_job = lambda job: events.append(job.name)

        def ready(job):
            if job.name == "camera":
                raise RuntimeError("camera not ready")

        launcher.wait_ready = ready
        with self.assertRaisesRegex(RuntimeError, "camera not ready"):
            launcher.start()
        self.assertEqual(events, ["bringup", "camera"])

    def test_ready_timeout_and_an_earlier_dead_process_are_errors(self):
        ticks = iter([0, 0, 3])
        launcher = DEMO.Launcher(self.config, clock=lambda: next(ticks), sleep=lambda _: None)
        launcher.panes["bringup"] = "%1"
        launcher.alive = lambda name: True
        launcher.execute = lambda *a, **k: subprocess.CompletedProcess([], 1, "", "no scan")
        with self.assertRaisesRegex(RuntimeError, "준비 시간 초과"):
            launcher.wait_ready(self.jobs[0])
        launcher.clock = lambda: 0
        launcher.alive = lambda name: False
        with self.assertRaisesRegex(RuntimeError, "bringup"):
            launcher.wait_ready(self.jobs[1])

    def test_shutdown_keeps_api_and_pi_bridge_until_dependents_stop(self):
        events = []
        launcher = DEMO.Launcher(self.config, run=lambda *a, **k: subprocess.CompletedProcess([], 0), sleep=lambda _: None)
        launcher.panes.update({j.name: "%" + str(j.window) for j in self.jobs})
        launcher.alive = lambda name: name in launcher.panes
        launcher.execute = lambda *a, **k: events.append("web-stop")
        launcher.stop_job = lambda name: events.append(name) or True
        launcher.tmux = lambda *a, **k: events.append(a[0])
        launcher.save = lambda: None
        launcher.stop()
        self.assertEqual(events, ["set-hook", "web-stop", "patrol", "voice", "outputs", "bridge", "detector", "localization", "video", "camera", "bringup", "server", "kill-session"])
        self.assertTrue(self.config["stopped"])

    def test_hung_patrol_keeps_remaining_ros_and_server_available(self):
        launcher = DEMO.Launcher(self.config)
        launcher.panes["server"] = "%4"
        launcher.alive = lambda name: name in launcher.panes
        launcher.execute = lambda *a, **k: None
        calls = []
        launcher.stop_job = lambda name: calls.append(name) or False
        with self.assertRaisesRegex(RuntimeError, "순찰이 종료되지"):
            launcher.stop()
        self.assertEqual(calls, ["patrol"])

    def test_unreachable_probe_is_bounded(self):
        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

        launcher = DEMO.Launcher(self.config, run=timeout)
        result = launcher.execute("pi", "anything")
        self.assertEqual(result.returncode, 124)

    def test_hung_outputs_preserve_bridge_communications_and_run_state(self):
        launcher = DEMO.Launcher(self.config)
        launcher.panes.update({j.name: "%" + str(j.window) for j in self.jobs})
        launcher.alive = lambda name: name in launcher.panes
        events = []
        launcher.execute = lambda *a, **k: events.append("web-stop")
        launcher.stop_job = lambda name: events.append(name) or name != "outputs"
        launcher.tmux = lambda *a, **k: self.assertNotEqual(a[0], "kill-session", "tmux session must stay available")
        launcher.run = lambda *a, **k: self.fail("SSH connection must stay available")
        launcher.save = lambda: self.fail("shutdown must not be marked complete")
        with self.assertRaisesRegex(RuntimeError, "출력 중계가 종료되지"):
            launcher.stop()
        self.assertEqual(events, ["web-stop", "patrol", "voice", "outputs"])
        self.assertNotIn("stopped", self.config)

    def test_stop_can_be_retried_after_outputs_recover(self):
        launcher = DEMO.Launcher(self.config, sleep=lambda _: None)
        launcher.panes.update({j.name: "%" + str(j.window) for j in self.jobs})
        running = set(launcher.panes)
        events = []
        launcher.alive = lambda name: name in running
        launcher.execute = lambda *a, **k: events.append("web-stop")
        launcher.tmux = lambda *a, **k: events.append(a[0])
        launcher.run = lambda *a, **k: events.append("ssh-exit")
        launcher.save = lambda: events.append("save")
        outputs_ready = False

        def stop(name):
            if name not in running:
                return True
            events.append(name)
            if name == "outputs" and not outputs_ready:
                return False
            running.remove(name)
            return True

        launcher.stop_job = stop
        with self.assertRaisesRegex(RuntimeError, "출력 중계가 종료되지"):
            launcher.stop()
        self.assertIn("bridge", running)
        self.assertIn("bringup", running)
        self.assertIn("server", running)
        events.clear()
        outputs_ready = True
        launcher.stop()
        self.assertEqual(events, ["set-hook", "web-stop", "outputs", "bridge", "detector", "localization", "video", "camera", "bringup", "server", "kill-session", "ssh-exit", "save"])
        self.assertTrue(self.config["stopped"])
        self.assertFalse(running)

    def test_rejects_loopback_server_and_ssh_option_injection(self):
        for args in (["start", "--robot=-ProxyCommand=bad", "--server-url", "http://server.invalid:8000"],
                     ["start", "--robot", "robot@example.invalid", "--server-url", "http://127.0.0.1:8000"]):
            with self.assertRaises(SystemExit):
                DEMO.arguments(args)


if __name__ == "__main__":
    unittest.main()
