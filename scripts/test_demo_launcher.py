"""Launcher regression tests. No SSH, tmux server, ROS2, audio, or robot is started."""

import importlib.util
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("demo_launcher", Path(__file__).with_name("demo_launcher.py"))
DEMO = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = DEMO
SPEC.loader.exec_module(DEMO)


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
        self.assertEqual(events, ["web-stop", "patrol", "voice", "outputs", "bridge", "detector", "localization", "video", "camera", "bringup", "server", "kill-session"])
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
        launcher.tmux = lambda *a, **k: self.fail("tmux session must stay available")
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
        self.assertEqual(events, ["web-stop", "outputs", "bridge", "detector", "localization", "video", "camera", "bringup", "server", "kill-session", "ssh-exit", "save"])
        self.assertTrue(self.config["stopped"])
        self.assertFalse(running)

    def test_rejects_loopback_server_and_ssh_option_injection(self):
        for args in (["start", "--robot=-ProxyCommand=bad", "--server-url", "http://server.invalid:8000"],
                     ["start", "--robot", "robot@example.invalid", "--server-url", "http://127.0.0.1:8000"]):
            with self.assertRaises(SystemExit):
                DEMO.arguments(args)


if __name__ == "__main__":
    unittest.main()
