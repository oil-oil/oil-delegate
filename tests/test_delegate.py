from __future__ import annotations

import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "delegate.py"
SPEC = importlib.util.spec_from_file_location("delegate_under_test", SCRIPT_PATH)
DELEGATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DELEGATE)
RUNNERS = ("claude", "workbuddy", "antigravity", "codex", "opencode", "dsh", "grok", "kimi")

FAKE_CLI = r'''#!{python}
import json
import os
import sys
import time

runner = {runner!r}
args = sys.argv[1:]
if os.environ.get("FAKE_CALL_LOG"):
    with open(os.environ["FAKE_CALL_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"runner": runner, "argv": args}}) + "\n")
if "--version" in args:
    print("fake-" + runner + "-1.0")
    raise SystemExit(0)
if runner == "dsh" and "--help" in args:
    if os.environ.get("FAKE_DSH_LEGACY") == "1":
        print("--json --session-id")
    else:
        print("--json --session-id --permission-mode --approval-policy")
    raise SystemExit(0)
if runner == "kimi" and args == ["session", "list", "--all", "--json"]:
    if os.environ.get("FAKE_KIMI_EXIT"):
        sys.stderr.write("cannot list kimi sessions\n")
        raise SystemExit(int(os.environ["FAKE_KIMI_EXIT"]))
    if os.environ.get("FAKE_RECORD"):
        with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as handle:
            json.dump({{"argv": args, "kimi_home": os.environ.get("KIMI_CODE_HOME")}}, handle)
    fixture = os.environ.get("FAKE_KIMI_SESSIONS")
    if fixture:
        with open(fixture, encoding="utf-8") as handle:
            sys.stdout.write(handle.read())
    else:
        print("[]")
    raise SystemExit(0)

stdin_text = sys.stdin.read()
record = os.environ.get("FAKE_RECORD")
if record:
    with open(record, "w", encoding="utf-8") as handle:
        json.dump({{
            "runner": runner,
            "argv": args,
            "stdin": stdin_text,
            "cwd": os.getcwd(),
            "permission": os.environ.get("OPENCODE_PERMISSION"),
            "dsh_mode": os.environ.get("DSH_PERMISSION_MODE"),
            "disable_background": os.environ.get("CODEBUDDY_CODE_DISABLE_BACKGROUND_TASKS"),
            "codebuddy_sandbox": os.environ.get("CODEBUDDY_IS_SANDBOX"),
        }}, handle)
if os.environ.get("FAKE_SLEEP"):
    time.sleep(float(os.environ["FAKE_SLEEP"]))
if os.environ.get("FAKE_FAIL") == "1":
    sys.stderr.write("fake failure\n")
    raise SystemExit(2)

session = None
for flag in ("--resume", "--conversation", "--session", "--session-id", "-S"):
    if flag in args:
        session = args[args.index(flag) + 1]
if runner == "codex" and "resume" in args:
    session = args[-2]
session = session or ("fake-" + runner + "-session")

if os.environ.get("FAKE_DSH_BAD_FINAL") == "1":
    print(json.dumps({{"type": "session", "sessionId": session}}))
    print(json.dumps({{"type": "final", "sessionId": session, "text": "", "reason": {{"kind": "error", "error": {{"message": "bad final"}}}}}}))
    raise SystemExit(0)

if os.environ.get("FAKE_EMPTY") == "1":
    if runner in ("claude", "workbuddy"):
        print(json.dumps({{"session_id": session, "result": "", "is_error": False}}))
    elif runner == "antigravity":
        print(json.dumps({{"conversation_id": session, "status": "SUCCESS", "response": ""}}))
    elif runner == "grok":
        print(json.dumps({{"text": "", "stopReason": "end_turn", "sessionId": session}}))
    elif runner == "kimi":
        print(json.dumps({{"type": "session.resume_hint", "session_id": session}}))
    elif runner == "codex":
        print(json.dumps({{"type": "thread.started", "thread_id": session}}))
    elif runner == "opencode":
        print(json.dumps({{"type": "init", "sessionID": session}}))
    elif runner == "dsh":
        print(json.dumps({{"type": "session", "sessionId": session}}))
        print(json.dumps({{"type": "status", "phase": "turn_end", "reason": {{"kind": "completed"}}}}))
        print(json.dumps({{"type": "final", "text": ""}}))
    raise SystemExit(0)

if os.environ.get("FAKE_CODEX_FAIL_NO_DETAIL") == "1":
    print(json.dumps({{"type": "thread.started", "thread_id": session}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "partial"}}}}))
    print(json.dumps({{"type": "turn.failed"}}))
    raise SystemExit(0)

if runner in ("claude", "workbuddy"):
    print(json.dumps({{"session_id": session, "result": "fake response", "is_error": False}}))
elif runner == "antigravity":
    print(json.dumps({{"conversation_id": session, "status": "SUCCESS", "response": "fake response"}}))
elif runner == "grok":
    print(json.dumps({{"text": "fake response", "stopReason": "end_turn", "sessionId": session, "requestId": "fake-request"}}))
elif runner == "kimi":
    print(json.dumps({{"role": "assistant", "content": "fake response"}}))
    print(json.dumps({{"type": "session.resume_hint", "session_id": session}}))
elif runner == "codex":
    print(json.dumps({{"type": "thread.started", "thread_id": session}}))
    print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "fake response"}}}}))
elif runner == "opencode":
    print(json.dumps({{"type": "text", "sessionID": session, "part": {{"text": "fake response"}}}}))
elif runner == "dsh":
    print(json.dumps({{"type": "session", "sessionId": session}}))
    print(json.dumps({{"type": "text", "text": "fake response"}}))
    print(json.dumps({{"type": "status", "phase": "turn_end", "reason": {{"kind": "completed"}}}}))
    print(json.dumps({{"type": "final", "text": "fake response"}}))
'''


class DelegateTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name).resolve()
        self.state_dir = self.root / "state"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.binaries = {runner: self.make_fake_cli(runner) for runner in RUNNERS}

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def make_fake_cli(self, runner: str) -> Path:
        path = self.bin_dir / runner
        path.write_text(FAKE_CLI.format(python=sys.executable, runner=runner), encoding="utf-8")
        path.chmod(0o755)
        return path

    def base_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["OIL_DELEGATE_HOME"] = str(self.state_dir)
        # Every data root points into the fixture, including official CLI subprocesses.
        env["HOME"] = str(self.root / "home")
        env["CODEX_HOME"] = str(self.root / "codex-home")
        env["KIMI_CODE_HOME"] = str(self.root / "kimi-home")
        env["OIL_DELEGATE_ANTIGRAVITY_CLI_HOME"] = str(self.root / "anti-cli-home")
        env["OIL_DELEGATE_ANTIGRAVITY_HOME"] = str(self.root / "anti-home")
        for key in list(env):
            if key.startswith("FAKE_"):
                env.pop(key, None)
        for runner, path in self.binaries.items():
            env[f"OIL_DELEGATE_{runner.upper()}_BIN"] = str(path)
        return env

    def run_cli(
        self,
        *args: str,
        extra_env: dict[str, str] | None = None,
        remove_env: list[str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self.base_env()
        for key in remove_env or []:
            env.pop(key, None)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(SCRIPT_PATH), *args],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )

    def onboard_all(self) -> None:
        result = self.run_cli(
            "onboard",
            "--enable",
            ",".join(RUNNERS),
            "--default-runner",
            "ask",
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)

    def run_task(
        self,
        runner: str,
        mode: str = "read",
        session: str | None = None,
        extra_env: dict[str, str] | None = None,
        timeout: str = "20",
    ) -> subprocess.CompletedProcess[str]:
        args = [
            "run",
            "--runner",
            runner,
            "--workspace",
            str(self.workspace),
            "--mode",
            mode,
            "--prompt",
            "inspect the fixture",
            "--timeout",
            timeout,
        ]
        if session:
            args.extend(["--session", session])
        return self.run_cli(*args, extra_env=extra_env)

    def result_of(self, proc: subprocess.CompletedProcess[str]) -> dict:
        self.assertIn("result_path=", proc.stdout, msg=proc.stdout + proc.stderr)
        values = dict(
            line.split("=", 1)
            for line in proc.stdout.splitlines()
            if "=" in line
        )
        return json.loads(Path(values["result_path"]).read_text(encoding="utf-8"))

    def record_of(self) -> dict:
        return json.loads((self.root / "record.json").read_text(encoding="utf-8"))

    def test_scan_is_read_only_and_reports_eight_runners(self) -> None:
        result = self.run_cli("onboard", "--scan")
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["status"], "NEEDS_ONBOARDING")
        self.assertEqual(set(payload["installed_runners"]), set(RUNNERS))
        self.assertTrue(payload["detected_runners"]["dsh"]["capabilities"]["json"])
        self.assertTrue(payload["detected_runners"]["dsh"]["capabilities"]["resume"])
        self.assertTrue(payload["detected_runners"]["dsh"]["capabilities"]["permission_flag"])
        self.assertFalse((self.state_dir / "config.json").exists())

    def test_onboarding_choice_alias_default_and_merge(self) -> None:
        record_path = self.root / "record.json"
        before = self.run_task("claude", extra_env={"FAKE_RECORD": str(record_path)})
        self.assertEqual(before.returncode, 10)
        before_payload = json.loads(before.stdout)
        self.assertEqual(before_payload["status"], "NEEDS_ONBOARDING")
        self.assertEqual(set(before_payload["installed_runners"]), set(RUNNERS))
        self.assertIn("detected_runners", before_payload)
        self.assertFalse((self.state_dir / "config.json").exists())
        self.assertFalse(record_path.exists())

        onboard = self.run_cli("onboard", "--enable", "claude,dsh", "--default-runner", "ask")
        self.assertEqual(onboard.returncode, 0, msg=onboard.stderr)
        choice = self.run_cli(
            "run",
            "--workspace",
            str(self.workspace),
            "--mode",
            "read",
            "--prompt",
            "inspect",
        )
        self.assertEqual(choice.returncode, 11)
        self.assertEqual(json.loads(choice.stdout)["status"], "NEEDS_RUNNER_CHOICE")

        configured = self.run_cli("config", "--set-default-runner", "agy")
        self.assertEqual(configured.returncode, 0, msg=configured.stderr)
        self.assertEqual(json.loads(configured.stdout)["default_runner"], "antigravity")
        default_run = self.run_cli(
            "run",
            "--workspace",
            str(self.workspace),
            "--mode",
            "read",
            "--prompt",
            "inspect",
        )
        self.assertEqual(default_run.returncode, 0, msg=default_run.stderr)
        self.assertEqual(self.result_of(default_run)["runner"], "antigravity")

        config_path = self.state_dir / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["future_field"] = {"keep": True}
        config_path.write_text(json.dumps(config), encoding="utf-8")
        again = self.run_cli(
            "onboard",
            "--enable",
            "claude,codebuddy,deepseek-harness,agy,claude",
            "--default-runner",
            "claude-code",
        )
        self.assertEqual(again.returncode, 0, msg=again.stderr)
        merged = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(merged["future_field"], {"keep": True})
        self.assertEqual(merged["default_runner"], "claude")
        self.assertEqual(len(merged["enabled_runners"]), len(set(merged["enabled_runners"])))

    def test_short_aliases_work_in_onboarding_run_and_config(self) -> None:
        onboard = self.run_cli(
            "onboard",
            "--enable",
            "cc,wb,agy,dsh,claude,grok-build,kimi-code",
            "--default-runner",
            "CC",
        )
        self.assertEqual(onboard.returncode, 0, msg=onboard.stderr)
        payload = json.loads(onboard.stdout)
        self.assertEqual(
            payload["enabled_runners"],
            ["claude", "workbuddy", "antigravity", "dsh", "grok", "kimi"],
        )
        self.assertEqual(payload["default_runner"], "claude")
        self.assertIn("cc", payload["detected_runners"]["claude"]["aliases"])
        self.assertIn("wb", payload["detected_runners"]["workbuddy"]["aliases"])
        self.assertIn("anti", payload["detected_runners"]["antigravity"]["aliases"])
        self.assertIn("grok-build", payload["detected_runners"]["grok"]["aliases"])
        self.assertIn("kimi-code", payload["detected_runners"]["kimi"]["aliases"])

        for alias, canonical in (("wb", "workbuddy"), ("kimi-code", "kimi"), ("anti", "antigravity")):
            with self.subTest(alias=alias):
                proc = self.run_task(alias, mode="write")
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(self.result_of(proc)["runner"], canonical)

        configured = self.run_cli("config", "--set-default-runner", "WB")
        self.assertEqual(configured.returncode, 0, msg=configured.stderr)
        self.assertEqual(json.loads(configured.stdout)["default_runner"], "workbuddy")

        binary = self.run_cli("config", "--set-bin", f"CC={self.binaries['claude']}")
        self.assertEqual(binary.returncode, 0, msg=binary.stderr)
        config_path = self.state_dir / "config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["runner_paths"]["claude"], str(self.binaries["claude"]))
        cleared = self.run_cli("config", "--clear-bin", "cc")
        self.assertEqual(cleared.returncode, 0, msg=cleared.stderr)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertNotIn("claude", config["runner_paths"])

    def test_disabled_runner_is_not_silently_substituted(self) -> None:
        onboard = self.run_cli("onboard", "--enable", "claude", "--default-runner", "claude")
        self.assertEqual(onboard.returncode, 0, msg=onboard.stderr)
        result = self.run_task("codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not enabled", result.stderr)

    def test_all_runner_modes_and_resume(self) -> None:
        self.onboard_all()
        expected_read = {
            "claude": ["--permission-mode", "plan"],
            "workbuddy": ["--permission-mode", "plan"],
            "antigravity": ["--mode", "plan"],
            "codex": ["--sandbox", "read-only"],
            "opencode": ["--agent", "plan"],
            "dsh": ["--permission-mode", "read-only"],
            "grok": ["--permission-mode", "plan"],
        }
        expected_write = {
            "claude": ["--dangerously-skip-permissions"],
            "workbuddy": ["--permission-mode", "bypassPermissions"],
            "antigravity": ["--dangerously-skip-permissions"],
            "codex": ["--dangerously-bypass-approvals-and-sandbox"],
            "opencode": [],
            "dsh": ["--permission-mode", "danger-full-access"],
            "grok": ["--always-approve"],
            "kimi": [],
        }
        resume_flags = {
            "claude": "--resume",
            "workbuddy": "--resume",
            "antigravity": "--conversation",
            "codex": "resume",
            "opencode": "--session",
            "dsh": "--session-id",
            "grok": "--resume",
            "kimi": "-S",
        }

        for runner in RUNNERS:
            if runner == "kimi":
                with self.subTest(runner=runner, mode="read_unsupported"):
                    proc = self.run_task(runner)
                    self.assertEqual(proc.returncode, 1)
                    self.assertIn("cannot guarantee read-only", proc.stderr)
            else:
                with self.subTest(runner=runner, mode="read"):
                    record_path = self.root / "record.json"
                    proc = self.run_task(runner, extra_env={"FAKE_RECORD": str(record_path)})
                    self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                    result = self.result_of(proc)
                    record = self.record_of()
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(result["response"], "fake response")
                    self.assertEqual(result["session_id"], f"fake-{runner}-session")
                    self.assertEqual(record["cwd"], str(self.workspace))
                    self.assertIn(expected_read[runner], [record["argv"][i:i + len(expected_read[runner])] for i in range(len(record["argv"]))])
                    self.assertFalse(any("inspect the fixture" in part for part in result["command"]))
                    if runner in ("codex", "dsh"):
                        self.assertEqual(result["command"][-1], "-")
                    else:
                        self.assertIn("<prompt>", result["command"])
                    for artifact in (result["output_path"], result["events_path"], result["raw_stdout_path"], result["stderr_path"]):
                        self.assertTrue(Path(artifact).is_file())
                    if runner == "opencode":
                        self.assertEqual(json.loads(record["permission"])["edit"], "deny")
                        self.assertEqual(json.loads(record["permission"])["bash"], "deny")
                    if runner == "workbuddy":
                        self.assertEqual(record["disable_background"], "1")

            with self.subTest(runner=runner, mode="write"):
                record_path = self.root / "record.json"
                proc = self.run_task(runner, mode="write", extra_env={"FAKE_RECORD": str(record_path)})
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                write_result = self.result_of(proc)
                record = self.record_of()
                if expected_write[runner]:
                    flags = expected_write[runner]
                    self.assertIn(flags, [record["argv"][i:i + len(flags)] for i in range(len(record["argv"]))])
                if runner == "opencode":
                    self.assertEqual(json.loads(record["permission"])["edit"], "allow")
                    self.assertEqual(json.loads(record["permission"])["bash"], "allow")
                    self.assertEqual(json.loads(record["permission"])["external_directory"], "allow")
                if runner == "dsh":
                    self.assertIn("--approval-policy", record["argv"])
                    self.assertIn("never", record["argv"])
                if runner == "workbuddy":
                    self.assertEqual(record["codebuddy_sandbox"], "1")
                if runner == "kimi":
                    self.assertNotIn("--auto", record["argv"])
                    self.assertIn("stream-json", record["argv"])

            with self.subTest(runner=runner, mode="resume"):
                record_path = self.root / "record.json"
                proc = self.run_task(
                    runner,
                    mode="write" if runner == "kimi" else "read",
                    session=write_result["session_id"],
                    extra_env={"FAKE_RECORD": str(record_path)},
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                resumed = self.result_of(proc)
                record = self.record_of()
                self.assertEqual(resumed["session_id"], write_result["session_id"])
                self.assertIn(resume_flags[runner], record["argv"])
                if runner == "codex":
                    self.assertIn('sandbox_mode="read-only"', record["argv"])

    def test_default_mode_is_full_access(self) -> None:
        args = DELEGATE.build_parser().parse_args(["run", "--prompt", "inspect"])
        self.assertEqual(args.mode, "write")

        self.onboard_all()
        record_path = self.root / "record.json"
        proc = self.run_cli(
            "run",
            "--runner",
            "grok",
            "--workspace",
            str(self.workspace),
            "--prompt",
            "use default permissions",
            extra_env={"FAKE_RECORD": str(record_path)},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(self.result_of(proc)["mode"], "write")
        record = self.record_of()
        self.assertIn("--always-approve", record["argv"])

    def test_dsh_legacy_permission_flags_use_environment(self) -> None:
        self.onboard_all()
        for mode, expected in (("read", "read-only"), ("write", "danger-full-access")):
            with self.subTest(mode=mode):
                record_path = self.root / "record.json"
                proc = self.run_task(
                    "dsh",
                    mode=mode,
                    extra_env={"FAKE_RECORD": str(record_path), "FAKE_DSH_LEGACY": "1"},
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                record = self.record_of()
                self.assertEqual(record["dsh_mode"], expected)
                self.assertNotIn("--permission-mode", record["argv"])

    def test_prompt_does_not_forbid_git_operations(self) -> None:
        prompt = DELEGATE.compose_prompt("inspect", self.workspace, "write", [])
        self.assertNotIn("Do not run git add", prompt)
        self.assertNotIn("git commit", prompt)
        self.assertNotIn("git push", prompt)
        self.assertNotIn("publish", prompt)
        self.assertIn("Final report", prompt)

    def test_dry_run_has_no_process_or_run_artifacts(self) -> None:
        self.onboard_all()
        record_path = self.root / "record.json"
        result = self.run_cli(
            "run",
            "--runner",
            "claude",
            "--workspace",
            str(self.workspace),
            "--mode",
            "read",
            "--prompt",
            "do not execute",
            "--dry-run",
            extra_env={"FAKE_RECORD": str(record_path)},
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertEqual(json.loads(result.stdout)["status"], "DRY_RUN")
        self.assertFalse(record_path.exists())
        self.assertFalse((self.state_dir / "runs").exists())

    def test_failure_timeout_and_dsh_bad_final(self) -> None:
        self.onboard_all()
        failed = self.run_task("claude", extra_env={"FAKE_FAIL": "1"})
        self.assertEqual(failed.returncode, 2)
        failed_result = self.result_of(failed)
        self.assertEqual(failed_result["status"], "failed")
        self.assertIn("fake failure", failed_result["error"])

        timed = self.run_task(
            "antigravity",
            timeout="1",
            extra_env={"FAKE_SLEEP": "3"},
        )
        self.assertEqual(timed.returncode, 124)
        self.assertEqual(self.result_of(timed)["status"], "failed")

        bad_final = self.run_task("dsh", extra_env={"FAKE_DSH_BAD_FINAL": "1"})
        self.assertEqual(bad_final.returncode, 1)
        bad_result = self.result_of(bad_final)
        self.assertEqual(bad_result["status"], "failed")
        self.assertIn("bad final", bad_result["error"])

    def test_json_output_contract_and_resume_commands(self) -> None:
        self.onboard_all()
        # Fake CLIs live at custom paths, so only the arguments after the executable are compared.
        expected = {
            "claude": "--resume fake-claude-session",
            "antigravity": "--conversation fake-antigravity-session",
            "codex": "resume fake-codex-session",
            "opencode": "--session fake-opencode-session",
            "dsh": "tui --resume fake-dsh-session",
            "grok": "--resume fake-grok-session",
            "kimi": "-S fake-kimi-session",
        }
        for runner, command in expected.items():
            mode = "write" if runner == "kimi" else "read"
            proc = self.run_cli(
                "run", "--runner", runner, "--workspace", str(self.workspace), "--mode", mode,
                "--prompt", "inspect", "--json",
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["contract_version"], 1)
            self.assertEqual(payload["status"], "completed")
            self.assertEqual(payload["runner"], runner)
            self.assertTrue(payload["resume_command"].endswith(" " + command), msg=payload["resume_command"])
            stored = json.loads(Path(payload["result_path"]).read_text(encoding="utf-8"))
            self.assertEqual(stored["resume_command"], payload["resume_command"])

        dry = json.loads(
            self.run_cli(
                "run", "--runner", "agy", "--workspace", str(self.workspace), "--prompt", "x",
                "--dry-run", "--json",
            ).stdout
        )
        self.assertEqual((dry["status"], dry["runner"], dry["contract_version"]), ("DRY_RUN", "antigravity", 1))

    def test_json_mode_reports_errors_and_keeps_exit_codes(self) -> None:
        self.onboard_all()
        refused = self.run_cli(
            "run", "--runner", "kimi", "--workspace", str(self.workspace), "--mode", "read",
            "--prompt", "x", "--dry-run", "--json",
        )
        self.assertEqual(refused.returncode, 1)
        payload = json.loads(refused.stdout)
        self.assertEqual(payload["status"], "ERROR")
        self.assertTrue(payload["error"])

        timed = self.run_cli(
            "run", "--runner", "claude", "--workspace", str(self.workspace), "--prompt", "x",
            "--timeout", "1", "--json", extra_env={"FAKE_SLEEP": "3"},
        )
        self.assertEqual(timed.returncode, 124)
        self.assertEqual(json.loads(timed.stdout)["status"], "failed")

        choice = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "x", "--json")
        self.assertEqual(choice.returncode, 11)
        self.assertEqual(json.loads(choice.stdout)["status"], "NEEDS_RUNNER_CHOICE")

    def test_parallel_runs_use_distinct_result_paths(self) -> None:
        self.onboard_all()
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    str(SCRIPT_PATH),
                    "run",
                    "--runner",
                    "claude",
                    "--workspace",
                    str(self.workspace),
                    "--mode",
                    "read",
                    "--prompt",
                    f"task {index}",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=self.base_env(),
            )
            for index in range(2)
        ]
        results = []
        for proc in processes:
            stdout, stderr = proc.communicate(timeout=20)
            self.assertEqual(proc.returncode, 0, msg=stderr)
            values = dict(line.split("=", 1) for line in stdout.splitlines() if "=" in line)
            results.append(values["result_path"])
        self.assertNotEqual(results[0], results[1])
        self.assertTrue(all(Path(path).is_file() for path in results))

    def test_secret_environment_is_not_copied_into_result(self) -> None:
        self.onboard_all()
        sentinel = "oil-delegate-test-sentinel"
        proc = self.run_task("dsh", extra_env={"DEEPSEEK_API_KEY": sentinel})
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = self.result_of(proc)
        self.assertNotIn(sentinel, json.dumps(result))
        self.assertNotIn(sentinel, Path(result["events_path"]).read_text(encoding="utf-8"))

    def test_entry_file_is_validated_and_included_only_in_prompt(self) -> None:
        self.onboard_all()
        missing = self.run_cli(
            "run",
            "--runner",
            "claude",
            "--workspace",
            str(self.workspace),
            "--mode",
            "read",
            "--prompt",
            "inspect",
            "--file",
            "missing.py",
        )
        self.assertEqual(missing.returncode, 1)
        entry = self.workspace / "entry.py"
        entry.write_text("print('ok')\n", encoding="utf-8")
        record_path = self.root / "record.json"
        valid = self.run_cli(
            "run",
            "--runner",
            "claude",
            "--workspace",
            str(self.workspace),
            "--mode",
            "read",
            "--prompt",
            "inspect",
            "--file",
            "entry.py",
            extra_env={"FAKE_RECORD": str(record_path)},
        )
        self.assertEqual(valid.returncode, 0, msg=valid.stderr)
        record = self.record_of()
        self.assertIn(str(entry), record["stdin"] + " ".join(record["argv"]))
        result = self.result_of(valid)
        self.assertNotIn(str(entry), " ".join(result["command"]))

    def test_empty_responses_are_failures_for_all_runners(self) -> None:
        self.onboard_all()
        for runner in RUNNERS:
            with self.subTest(runner=runner):
                proc = self.run_task(
                    runner,
                    mode="write" if runner == "kimi" else "read",
                    extra_env={"FAKE_EMPTY": "1"},
                )
                self.assertNotEqual(proc.returncode, 0)
                result = self.result_of(proc)
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["error"])

    def test_codex_failure_without_detail_is_not_success(self) -> None:
        self.onboard_all()
        proc = self.run_task("codex", extra_env={"FAKE_CODEX_FAIL_NO_DETAIL": "1"})
        self.assertNotEqual(proc.returncode, 0)
        result = self.result_of(proc)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Codex turn failed", result["error"])

    def test_set_bin_and_default_runner_in_same_request(self) -> None:
        missing = self.run_cli(
            "config",
            "--set-default-runner",
            "opencode",
            remove_env=["OIL_DELEGATE_OPENCODE_BIN"],
        )
        self.assertEqual(missing.returncode, 1)
        self.assertFalse((self.state_dir / "config.json").exists())

        configured = self.run_cli(
            "config",
            "--set-bin",
            f"opencode={self.binaries['opencode']}",
            "--set-default-runner",
            "opencode",
            remove_env=["OIL_DELEGATE_OPENCODE_BIN"],
        )
        self.assertEqual(configured.returncode, 0, msg=configured.stderr)
        payload = json.loads(configured.stdout)
        self.assertEqual(payload["default_runner"], "opencode")
        config = json.loads((self.state_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["runner_paths"]["opencode"], str(self.binaries["opencode"]))

    def test_config_and_runner_choice_skip_version_probes(self) -> None:
        call_log = self.root / "calls.jsonl"
        self.run_cli("onboard", "--enable", ",".join(RUNNERS), "--default-runner", "ask", extra_env={"FAKE_CALL_LOG": str(call_log)})
        call_log.unlink()

        configured = self.run_cli("config")
        self.assertEqual(configured.returncode, 0, msg=configured.stderr)
        self.assertFalse(call_log.exists())

        choice = self.run_cli("run", "--workspace", str(self.workspace), "--prompt", "x")
        self.assertEqual(choice.returncode, 11)
        self.assertFalse(call_log.exists())

    def test_stdout_aliases_share_one_file_and_no_timeout_config(self) -> None:
        self.onboard_all()
        proc = self.run_task("claude")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        result = self.result_of(proc)
        self.assertEqual(result["events_path"], result["raw_stdout_path"])
        self.assertTrue(Path(result["events_path"]).is_file())
        self.assertNotIn("run_timeout_seconds", DELEGATE.default_config())

    def write_codex_index(self, rows: list[dict]) -> Path:
        path = self.root / "codex-home" / "session_index.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def write_rollout(self, sid: str, cwd: str) -> Path:
        path = self.root / "codex-home" / "sessions" / "2026" / "11" / "03" / f"rollout-2026-11-03-{sid}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        header = {"type": "session_meta", "payload": {"id": sid, "cwd": cwd}}
        # Invalid UTF-8 and JSON after the header must never be decoded or parsed.
        path.write_bytes(json.dumps(header).encode() + b"\n\xffnot conversation metadata\n")
        return path

    def write_anti_db(self, dirname: str, rows: list[tuple], *, wal: bool = False) -> sqlite3.Connection:
        path = self.root / dirname / "conversation_summaries.db"
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path)
        if wal:
            connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT, "
            "workspace_uris TEXT, last_modified_time, body TEXT DEFAULT 'private')"
        )
        connection.executemany("INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, 'private')", rows)
        connection.commit()
        if wal:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return connection

    def sessions_payload(self, *args: str, extra_env: dict[str, str] | None = None) -> dict:
        proc = self.run_cli("sessions", *args, "--json", extra_env=extra_env)
        self.assertEqual(proc.returncode, 0, msg=proc.stdout + proc.stderr)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["contract_version"], 1)
        self.assertEqual(payload["status"], "OK")
        self.assertIsInstance(payload["skipped"], dict)
        return payload

    def test_sessions_kimi_official_list_and_root_override_without_onboarding(self) -> None:
        fixture = self.root / "kimi-sessions.json"
        fixture.write_text(json.dumps([
            {"id": "kimi-1", "title": "项目 Plan", "workDir": "/fixture/project", "updatedAt": "1793664000123"},
            {"id": "kimi-2", "title": "Unrelated", "workDir": "/fixture/other", "updatedAt": "1793665000123",
             "firstPrompt": "项目 Plan"},
        ]))
        log = self.root / "calls.jsonl"
        record = self.root / "record.json"
        payload = self.sessions_payload("--runner", "kimi-code", "--query", "项目 pLAN", extra_env={
            "FAKE_KIMI_SESSIONS": str(fixture), "FAKE_CALL_LOG": str(log), "FAKE_RECORD": str(record),
        })
        row, = payload["sessions"]
        self.assertEqual(set(row), {"runner", "session_id", "title", "workspace", "updated_at", "resume_command", "source"})
        self.assertEqual((row["runner"], row["session_id"], row["title"], row["workspace"], row["source"]),
                         ("kimi", "kimi-1", "项目 Plan", "/fixture/project", "official"))
        self.assertEqual(row["updated_at"], "2026-11-03T00:00:00.123000+00:00")
        self.assertEqual(shlex.split(row["resume_command"])[1:], ["-S", "kimi-1"])
        self.assertEqual(json.loads(log.read_text())["argv"], ["session", "list", "--all", "--json"])
        self.assertEqual(self.record_of()["kimi_home"], str(self.root / "kimi-home"))
        self.assertEqual((payload["unsupported"], payload["errors"]), ([], {}))
        self.assertEqual(payload["skipped"], {})
        self.assertFalse(self.state_dir.exists())

    def test_sessions_kimi_null_titles_and_bad_rows_keep_valid_sessions(self) -> None:
        bad_rows = [
            None,
            [],
            {"title": "Missing ID"},
            {"id": None, "title": "Null ID"},
            {"id": 42, "title": "Numeric ID"},
            {"id": "", "title": "Empty ID"},
            {"id": "bad-title", "title": False},
            {"id": "bad-time", "title": "Invalid time", "updatedAt": "invalid"},
            {"id": "bad-cwd", "title": "Invalid workspace", "workDir": []},
        ]
        fixture = self.root / "kimi-mixed.json"
        fixture.write_text(json.dumps([
            bad_rows[0],
            {"id": "named", "title": "Topic", "workDir": "/fixture/kimi", "updatedAt": "1793664000000"},
            {"id": "unnamed", "title": None, "updatedAt": "1793665000000"},
            *bad_rows[1:],
            {"id": "missing-title"},
        ]))
        env = {"FAKE_KIMI_SESSIONS": str(fixture)}
        payload = self.sessions_payload("--runner", "kimi", extra_env=env)
        rows = {row["session_id"]: row for row in payload["sessions"]}
        self.assertEqual(set(rows), {"named", "unnamed", "missing-title"})
        self.assertEqual(rows["unnamed"]["title"], "")
        self.assertEqual(rows["missing-title"]["title"], "")
        self.assertEqual(payload["errors"], {})
        self.assertEqual(payload["skipped"], {"kimi": len(bad_rows)})
        filtered = self.sessions_payload("--runner", "kimi", "--query", "topic", "--limit", "1", extra_env=env)
        self.assertEqual([row["session_id"] for row in filtered["sessions"]], ["named"])
        self.assertEqual(filtered["errors"], {})
        self.assertEqual(filtered["skipped"], payload["skipped"])

    def test_sessions_codex_renames_use_last_line_and_read_only_matching_headers(self) -> None:
        self.write_codex_index([
            {"id": "same-id", "thread_name": "Old title", "updated_at": "2026-11-03T12:00:00Z"},
            {"id": "other-id", "thread_name": "Unrelated", "updated_at": "2026-11-03T13:00:00Z"},
            # The last row wins even with an older timestamp.
            {"id": "same-id", "thread_name": "New title", "updated_at": "2026-11-03T11:00:00Z"},
        ])
        rollout = self.write_rollout("same-id", "/fixture/codex")
        unrelated = self.write_rollout("other-id", "/fixture/unrelated")
        unrelated.write_bytes(b"invalid header, must not be opened")
        before = rollout.read_bytes()
        payload = self.sessions_payload("--runner", "codex", "--query", "NEW TITLE")
        row, = payload["sessions"]
        self.assertEqual((row["session_id"], row["title"], row["workspace"], row["source"]),
                         ("same-id", "New title", "/fixture/codex", "local-store"))
        self.assertEqual(payload["errors"], {})
        self.assertEqual(shlex.split(row["resume_command"])[1:], ["resume", "same-id"])
        self.assertEqual(self.sessions_payload("--runner", "codex", "--query", "Old title")["sessions"], [])
        self.assertEqual(rollout.read_bytes(), before)

    def test_sessions_codex_hydrates_only_returned_results_after_limit(self) -> None:
        self.write_codex_index([
            {"id": "first", "thread_name": "Topic", "updated_at": "2026-11-03T12:00:00Z"},
            {"id": "second", "thread_name": "Topic", "updated_at": "2026-11-03T11:00:00Z"},
        ])
        self.write_rollout("first", "/fixture/first")
        self.write_rollout("second", "unused").write_bytes(b"unread invalid header")
        payload = self.sessions_payload("--runner", "codex", "--limit", "1")
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["first"])
        self.assertEqual(payload["errors"], {})

    def test_sessions_codex_bad_index_rows_do_not_discard_valid_rows(self) -> None:
        path = self.write_codex_index([
            {"id": "renamed", "thread_name": "Old title", "updated_at": None},
            None,
            [],
            {"thread_name": "Missing ID"},
            {"id": None, "thread_name": "Null ID"},
            {"id": 42, "thread_name": "Numeric ID"},
            {"id": "", "thread_name": "Empty ID"},
            {"id": "bad-title", "thread_name": []},
            {"id": "bad-time", "thread_name": "Invalid time", "updated_at": "invalid"},
        ])
        with path.open("a", encoding="utf-8") as handle:
            handle.write("invalid JSON\n\n")
            handle.write(json.dumps({"id": "renamed", "thread_name": "New title", "updated_at": None}) + "\n")
            handle.write(json.dumps({"id": "unnamed", "thread_name": None, "updated_at": None}) + "\n")
        payload = self.sessions_payload("--runner", "codex")
        self.assertEqual({row["session_id"]: row["title"] for row in payload["sessions"]},
                         {"renamed": "New title", "unnamed": ""})
        self.assertEqual(payload["errors"], {})
        self.assertEqual(payload["skipped"], {"codex": 9})

    def test_sessions_codex_without_rollout_keeps_metadata(self) -> None:
        self.write_codex_index([{"id": "missing", "thread_name": "Indexed", "updated_at": None}])
        row, = self.sessions_payload("--runner", "codex")["sessions"]
        self.assertIsNone(row["workspace"])
        self.assertIsNone(row["updated_at"])

    def test_sessions_codex_archived_or_deleted_rollouts_have_null_workspace(self) -> None:
        self.write_codex_index([
            {"id": sid, "thread_name": sid, "updated_at": None}
            for sid in ("active", "archived", "deleted")
        ])
        self.write_rollout("active", "/fixture/active")
        self.write_rollout("deleted", "/fixture/deleted").unlink()
        archive = self.root / "codex-home" / "archived_sessions" / "rollout-any-archived.jsonl.zst"
        archive.parent.mkdir()
        archive.write_bytes(b"archive must not be opened or decompressed")
        payload = self.sessions_payload("--runner", "codex")
        self.assertEqual({row["session_id"]: row["workspace"] for row in payload["sessions"]},
                         {"active": "/fixture/active", "archived": None, "deleted": None})
        self.assertEqual((payload["errors"], payload["skipped"]), ({}, {}))

    def test_sessions_preserves_submillisecond_order_and_default_local_root(self) -> None:
        path = self.write_codex_index([
            {"id": "a-older", "thread_name": "Topic", "updated_at": "2026-11-03T12:00:00.000001Z"},
            {"id": "z-newer", "thread_name": "Topic", "updated_at": "2026-11-03T12:00:00.000002Z"},
        ])
        default_root = self.root / "home" / ".codex"
        default_root.mkdir(parents=True)
        path.replace(default_root / path.name)
        proc = self.run_cli("sessions", "--runner", "codex", "--json", remove_env=["CODEX_HOME"])
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual([row["session_id"] for row in json.loads(proc.stdout)["sessions"]], ["z-newer", "a-older"])

    def test_sessions_antigravity_both_roots_dedup_uri_and_anti_alias(self) -> None:
        for dirname, rows in (
            ("anti-cli-home", [("shared", "Old", '["file:///fixture/old"]', "2026-11-03T01:00:00Z")]),
            ("anti-home", [("shared", "New", '["https://example.invalid", "file://localhost/fixture/a%20b"]',
                            "2026-11-03T03:00:00+01:00"),
                           ("unique", "Extra", '[]', 1793664000000)]),
        ):
            self.write_anti_db(dirname, rows).close()
        payload = self.sessions_payload("--runner", "ANTI")
        self.assertEqual(payload["unsupported"], [])
        self.assertEqual(payload["errors"], {})
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["shared", "unique"])
        row = payload["sessions"][0]
        self.assertEqual((row["runner"], row["title"], row["workspace"], row["updated_at"], row["source"]),
                         ("antigravity", "New", "/fixture/a b", "2026-11-03T02:00:00.000000+00:00", "local-store"))
        self.assertEqual(shlex.split(row["resume_command"])[1:], ["--conversation", "shared"])

    def test_sessions_antigravity_uses_immutable_snapshot_without_writes(self) -> None:
        connection = self.write_anti_db("anti cli ?#", [("checkpointed", "Saved", '[]', 1793664000)], wal=True)
        self.addCleanup(connection.close)
        connection.execute("INSERT INTO conversation_summaries VALUES ('wal-only', 'Recent', '[]', 1793665000, 'private')")
        connection.commit()
        root = self.root / "anti cli ?#"
        before = {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}
        connect = sqlite3.connect
        uris = []

        def immutable_connect(database: str, **kwargs):
            uris.append((database, kwargs))
            conn = connect(database, **kwargs)
            # Any attempt to select the body column violates the metadata-only contract.
            conn.set_authorizer(lambda op, table, column, *_:
                                sqlite3.SQLITE_DENY if op == sqlite3.SQLITE_READ and column == "body"
                                else sqlite3.SQLITE_OK)
            return conn

        with mock.patch.dict(os.environ, {**self.base_env(), "OIL_DELEGATE_ANTIGRAVITY_CLI_HOME": str(root)}, clear=True), \
                mock.patch.object(DELEGATE.sqlite3, "connect", side_effect=immutable_connect):
            errors = {}
            skipped = {}
            rows = DELEGATE.antigravity_sessions(errors, skipped)
        self.assertEqual(errors, {})
        self.assertEqual(skipped, {})
        self.assertEqual([row["session_id"] for row in rows], ["checkpointed"])
        self.assertEqual(uris, [((root / "conversation_summaries.db").as_uri() + "?immutable=1", {"uri": True})])
        self.assertEqual({p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in root.iterdir()}, before)

    def test_sessions_antigravity_bad_rows_keep_valid_rows_from_both_databases(self) -> None:
        self.write_anti_db("anti-cli-home", [
            ("first", "First topic", '["file:///fixture/first"]', 1793664000),
            (None, "Null ID", '[]', 1793664000),
            ("", "Empty ID", '[]', 1793664000),
            ("bad-json", "Invalid JSON", 'not JSON', 1793664000),
            ("unnamed", None, '[]', 1793664000),
        ]).close()
        self.write_anti_db("anti-home", [
            ("bad-list", "Invalid URI list", '{}', 1793664000),
            ("bad-uri", "Invalid URI", '[42]', 1793664000),
            ("bad-time", "Invalid time", '[]', 'invalid'),
            ("last", "Last topic", '["file:///fixture/last"]', 1793665000),
        ]).close()
        payload = self.sessions_payload("--runner", "anti")
        self.assertEqual({row["session_id"]: row["title"] for row in payload["sessions"]},
                         {"first": "First topic", "last": "Last topic", "unnamed": ""})
        self.assertEqual(payload["errors"], {})
        self.assertEqual(payload["skipped"], {"antigravity": 6})

    def test_sessions_one_antigravity_database_failure_keeps_other_database(self) -> None:
        bad = self.root / "anti-cli-home" / "conversation_summaries.db"
        bad.parent.mkdir()
        bad.write_bytes(b"not a database")
        self.write_anti_db("anti-home", [("valid", "Good", '[]', 1793664000)]).close()
        payload = self.sessions_payload("--runner", "anti")
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["valid"])
        self.assertIn("antigravity-cli", payload["errors"]["antigravity"])

    def test_sessions_source_failures_do_not_block_other_runners(self) -> None:
        index = self.write_codex_index([])
        index.unlink()
        index.mkdir()  # An actual source read failure, distinct from a skippable bad row.
        self.write_anti_db("anti-home", [("valid", "Good", '[]', 1793664000)]).close()
        payload = self.sessions_payload(extra_env={"FAKE_KIMI_EXIT": "2"})
        self.assertEqual([row["runner"] for row in payload["sessions"]], ["antigravity"])
        self.assertEqual(set(payload["errors"]), {"codex", "kimi"})
        self.assertIn("exited 2", payload["errors"]["kimi"])
        self.assertEqual(payload["skipped"], {})
        self.assertEqual(payload["unsupported"], ["claude", "workbuddy", "opencode", "dsh", "grok"])
        self.assertFalse(self.state_dir.exists())

    def test_sessions_exact_title_first_then_updated_at_across_runners(self) -> None:
        self.write_codex_index([{"id": "exact-old", "thread_name": "Topic", "updated_at": "2026-11-01T00:00:00Z"}])
        self.write_rollout("exact-old", "/fixture/codex")
        self.write_anti_db("anti-cli-home", [("partial-new", "A Topic", '[]', "2026-11-03T12:00:00Z")]).close()
        fixture = self.root / "kimi.json"
        fixture.write_text(json.dumps([
            {"id": "exact-new", "title": "TOPIC", "workDir": "/fixture/kimi", "updatedAt": "1793664000000"},
            {"id": "partial-old", "title": "Topic extra", "workDir": None, "updatedAt": "1793577600000"},
        ]))
        payload = self.sessions_payload("--query", "topic", "--limit", "3", extra_env={"FAKE_KIMI_SESSIONS": str(fixture)})
        self.assertEqual([row["session_id"] for row in payload["sessions"]], ["exact-new", "exact-old", "partial-new"])
        self.assertEqual(payload["errors"], {})

    def test_sessions_skips_symlinked_rollouts_and_directories(self) -> None:
        self.write_codex_index([{"id": "linked", "thread_name": "Linked", "updated_at": None}])
        external = self.root / "outside"
        external.mkdir()
        target = external / "rollout-any-linked.jsonl"
        target.write_bytes(b"must not be read")
        sessions = self.root / "codex-home" / "sessions"
        sessions.mkdir()
        (sessions / target.name).symlink_to(target)
        (sessions / "linked-directory").symlink_to(external, target_is_directory=True)
        payload = self.sessions_payload("--runner", "codex")
        self.assertIsNone(payload["sessions"][0]["workspace"])
        self.assertEqual(payload["errors"], {})

    def test_sessions_rejects_cloud_and_symlink_roots_before_scanning(self) -> None:
        real = self.root / "local"
        real.mkdir()
        symlink = self.root / "symlink"
        symlink.symlink_to(real, target_is_directory=True)
        for root in (symlink, self.root / "Library" / "CloudStorage" / "provider", self.root / "Screen Studio Archive"):
            with self.subTest(root=root):
                payload = self.sessions_payload("--runner", "codex", extra_env={"CODEX_HOME": str(root)})
                self.assertEqual(payload["sessions"], [])
                self.assertIn("codex", payload["errors"])

    def test_sessions_invalid_kimi_list_reports_source_error(self) -> None:
        fixture = self.root / "invalid-kimi.json"
        for content in ("not JSON", '{"sessions": "bad"}'):
            with self.subTest(content=content):
                fixture.write_text(content)
                payload = self.sessions_payload("--runner", "kimi", extra_env={"FAKE_KIMI_SESSIONS": str(fixture)})
                self.assertEqual(payload["sessions"], [])
                self.assertIn("kimi", payload["errors"])
                self.assertEqual(payload["skipped"], {})

    def test_sessions_missing_local_roots_are_empty(self) -> None:
        for runner in ("codex", "anti"):
            with self.subTest(runner=runner):
                payload = self.sessions_payload("--runner", runner)
                self.assertEqual((payload["sessions"], payload["errors"], payload["unsupported"]), ([], {}, []))

    def test_sessions_unsupported_runner_does_not_start_cli(self) -> None:
        log = self.root / "calls.jsonl"
        for runner, canonical in (("cc", "claude"), ("wb", "workbuddy"), ("opencode", "opencode"),
                                  ("dsh", "dsh"), ("grok-build", "grok")):
            with self.subTest(runner=runner):
                payload = self.sessions_payload("--runner", runner, extra_env={"FAKE_CALL_LOG": str(log)})
                self.assertEqual((payload["sessions"], payload["errors"], payload["unsupported"]), ([], {}, [canonical]))
        self.assertFalse(log.exists())

    def test_sessions_invalid_runner_and_limit_use_json_error_contract(self) -> None:
        for args in (("--runner", "unknown"), ("--limit", "0"), ("--limit", "-1")):
            with self.subTest(args=args):
                proc = self.run_cli("sessions", *args, "--json")
                self.assertEqual(proc.returncode, 1)
                payload = json.loads(proc.stdout)
                self.assertEqual((payload["contract_version"], payload["status"]), (1, "ERROR"))

    def test_sessions_plain_output_is_readable(self) -> None:
        self.write_codex_index([{"id": "one", "thread_name": "Title", "updated_at": None}])
        proc = self.run_cli("sessions", "--runner", "codex")
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertIn("codex\tone\tTitle\t", proc.stdout)


if __name__ == "__main__":
    unittest.main()
