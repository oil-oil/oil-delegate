from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "delegate.py"
RUNNERS = ("claude", "workbuddy", "antigravity", "codex", "opencode", "dsh", "grok", "kimi")

FAKE_CLI = r'''#!{python}
import json
import os
import sys
import time

runner = {runner!r}
args = sys.argv[1:]
if "--version" in args:
    print("fake-" + runner + "-1.0")
    raise SystemExit(0)
if runner == "dsh" and "--help" in args:
    if os.environ.get("FAKE_DSH_LEGACY") == "1":
        print("--json --session-id")
    else:
        print("--json --session-id --permission-mode --approval-policy")
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
    ) -> subprocess.CompletedProcess[str]:
        env = self.base_env()
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
        self.assertIn("grok-build", payload["detected_runners"]["grok"]["aliases"])
        self.assertIn("kimi-code", payload["detected_runners"]["kimi"]["aliases"])

        for alias, canonical in (
            ("wb", "workbuddy"),
            ("agy", "antigravity"),
            ("DSH", "dsh"),
            ("grok-build", "grok"),
            ("kimi-code", "kimi"),
        ):
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
                    self.assertNotIn("inspect the fixture", result["command"])
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
        self.onboard_all()
        for runner in ("grok", "kimi", "opencode", "dsh", "workbuddy"):
            with self.subTest(runner=runner):
                record_path = self.root / "record.json"
                proc = self.run_cli(
                    "run",
                    "--runner",
                    runner,
                    "--workspace",
                    str(self.workspace),
                    "--prompt",
                    "use default permissions",
                    extra_env={"FAKE_RECORD": str(record_path)},
                )
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertEqual(self.result_of(proc)["mode"], "write")
                record = self.record_of()
                if runner == "grok":
                    self.assertIn("--always-approve", record["argv"])
                if runner == "kimi":
                    self.assertNotIn("--auto", record["argv"])
                if runner == "opencode":
                    self.assertEqual(json.loads(record["permission"])["external_directory"], "allow")
                if runner == "dsh":
                    self.assertIn("danger-full-access", record["argv"])
                if runner == "workbuddy":
                    self.assertEqual(record["codebuddy_sandbox"], "1")

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
        self.onboard_all()
        record_path = self.root / "record.json"
        proc = self.run_task(
            "claude",
            mode="write",
            extra_env={"FAKE_RECORD": str(record_path)},
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        record = self.record_of()
        prompt = record["stdin"] + " ".join(record["argv"])
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


if __name__ == "__main__":
    unittest.main()
