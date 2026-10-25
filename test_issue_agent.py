import io
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch

from issue_agent import DEFAULT_MODEL, Workspace, main, run_agent, snapshot


class Item(SimpleNamespace):
    def model_dump(self, **kwargs):
        return vars(self)


def response(*items, text="", status="completed"):
    return SimpleNamespace(output=list(items), output_text=text, status=status,
                           usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def call(name, args, identifier="call_1"):
    return Item(type="function_call", name=name, arguments=json.dumps(args), call_id=identifier)


class AgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "app.py").write_text("def answer():\n    return 0\n", encoding="utf-8")
        (self.root / "test_app.py").write_text("assert True\n", encoding="utf-8")
        self.workspace = Workspace(self.root, "test-image")

    def test_function_call_output_has_matching_id(self):
        client = Mock()
        captured = []

        def create(**kwargs):
            captured.append(json.loads(json.dumps(kwargs)))
            if len(captured) == 1:
                return response(call("read_file", {"path": "app.py", "start": 1, "count": 20}))
            return response(text="No fix yet")

        client.responses.create.side_effect = create
        report = run_agent(client, self.workspace, "Fix answer")
        output = captured[1]["input"][-1]
        self.assertEqual(output["type"], "function_call_output")
        self.assertEqual(output["call_id"], "call_1")
        self.assertIn("return 0", output["output"])
        self.assertFalse(captured[0]["parallel_tool_calls"])
        self.assertEqual(report["input_tokens"], 200)
        self.assertFalse(report["verified"])

    def test_budget_prevents_request(self):
        client = Mock()
        report = run_agent(client, self.workspace, "Fix", token_budget=1)
        client.responses.create.assert_not_called()
        self.assertEqual(report["status"], "token_budget")

    def test_step_limit(self):
        client = Mock()
        client.responses.create.return_value = response(call("list_files", {}))
        report = run_agent(client, self.workspace, "Fix", max_steps=2)
        self.assertEqual(client.responses.create.call_count, 2)
        self.assertEqual(report["status"], "step_limit")
        self.assertEqual(report["max_steps"], 2)
        self.assertEqual(report["max_edits"], 3)

    def test_api_error_does_not_retry_or_expose_message(self):
        client = Mock()
        client.responses.create.side_effect = RuntimeError("sensitive detail")
        report = run_agent(client, self.workspace, "Fix")
        self.assertEqual(client.responses.create.call_count, 1)
        self.assertEqual(report["status"], "api_error")
        self.assertNotIn("sensitive detail", report["summary"])

    def test_incomplete_output_never_executes_tools(self):
        client = Mock()
        client.responses.create.return_value = response(
            call("replace_text", {"path": "app.py", "old": "return 0", "new": "return 1"}),
            status="incomplete")
        report = run_agent(client, self.workspace, "Fix")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(self.workspace.patch(), "")

    def test_tool_arguments_and_path_validation(self):
        cases = [("run_shell", "{}"), ("list_files", "not json"),
                 ("list_files", '{"extra": 1}'), ("list_files", "[]"),
                 ("read_file", '{"path":"../outside.py","start":1,"count":1}'),
                 ("read_file", '{"path":"app.py","start":true,"count":1}')]
        for name, arguments in cases:
            with self.subTest(name=name, arguments=arguments):
                self.assertIn("error", self.workspace.execute(name, arguments))

    def test_test_files_cannot_be_modified(self):
        result = self.workspace.execute("replace_text", json.dumps(
            {"path": "test_app.py", "old": "True", "new": "False"}))
        self.assertIn("error", result)

    def test_test_file_protection_is_case_insensitive(self):
        for name in ("TEST_other.py", "other_TEST.py", "CONFTEST.py", "Tests/helper.py"):
            with self.subTest(name=name):
                target = self.root / name
                target.parent.mkdir(exist_ok=True)
                target.write_text("value = 0\n", encoding="utf-8")
                workspace = Workspace(self.root, "test-image")
                with self.assertRaisesRegex(ValueError, "non-test"):
                    workspace.replace_text(name, "value = 0", "value = 1")
                self.assertEqual(workspace.edit_count, 0)

    def test_edit_invalidates_previous_verification(self):
        self.workspace.tests_passed = True
        self.workspace.replace_text("app.py", "return 0", "return 1")
        self.assertFalse(self.workspace.tests_passed)
        self.assertIn("+    return 1", self.workspace.patch())

    def test_snapshot_preserves_original_and_excludes_secrets(self):
        (self.root / ".env").write_text("OPENAI_API_KEY=secret", encoding="utf-8")
        (self.root / "credentials.txt").write_text("secret", encoding="utf-8")
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            snapshot(self.root.resolve(), destination)
            copied = Workspace(destination, "test-image")
            copied.replace_text("app.py", "return 0", "return 1")
            self.assertIn("return 0", (self.root / "app.py").read_text())
            self.assertFalse((destination / ".env").exists())
            self.assertFalse((destination / "credentials.txt").exists())

    def test_patch_handles_missing_final_newline(self):
        (self.root / "app.py").write_text("value = 0", encoding="utf-8")
        workspace = Workspace(self.root, "test-image")
        workspace.replace_text("app.py", "value = 0", "value = 1")
        self.assertIn("-value = 0\n\\ No newline at end of file\n+value = 1\n", workspace.patch())

    def test_snapshot_and_multiline_edit_preserve_line_endings(self):
        for newline in (b"\n", b"\r\n"):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as directory:
                original = newline.join((b"def answer():", b"    return 0", b""))
                (self.root / "app.py").write_bytes(original)
                destination = Path(directory)
                snapshot(self.root, destination)
                self.assertEqual((destination / "app.py").read_bytes(), original)
                workspace = Workspace(destination, "test-image")
                workspace.replace_text("app.py", "    return 0\n", "    value = 1\n    return value\n")
                expected = newline.join((b"def answer():", b"    value = 1", b"    return value", b""))
                self.assertEqual((destination / "app.py").read_bytes(), expected)
                self.assertIn("-    return 0" + newline.decode(), workspace.patch())
                self.assertEqual((self.root / "app.py").read_bytes(), original)

    def test_snapshot_accepts_relative_source_and_skips_nested_destination(self):
        # Avoid changing process cwd, which can affect other tests.
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            source = Path(directory)
            (source / "app.py").write_text("value = 0\n", encoding="utf-8")
            relative_source = Path(os.path.relpath(source, Path.cwd()))
            destination = source / "copied"
            snapshot(relative_source, destination)
            self.assertTrue((destination / "app.py").is_file())
            self.assertFalse((destination / "copied").exists())

    def test_snapshot_rejects_unsafe_destinations(self):
        for destination in (self.root, self.root.parent):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                snapshot(self.root, destination)
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            (destination / "keep.txt").write_text("keep", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "empty"):
                snapshot(self.root, destination)
            self.assertEqual((destination / "keep.txt").read_text(), "keep")

    def test_snapshot_rejects_empty_and_non_utf8_repositories(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            with self.assertRaisesRegex(ValueError, "No supported"):
                snapshot(source, Path(directory) / "empty")
            (source / "broken.py").write_bytes(b"\xff")
            with self.assertRaisesRegex(ValueError, "not UTF-8: broken.py"):
                snapshot(source, Path(directory) / "invalid")

    def test_missing_docker_is_not_a_pass(self):
        with patch("issue_agent.shutil.which", return_value=None):
            self.assertIn("error", self.workspace.run_tests())
        self.assertFalse(self.workspace.tests_passed)

    def test_mocked_full_repair_loop(self):
        client = Mock()
        client.responses.create.side_effect = [
            response(call("replace_text", {"path": "app.py", "old": "return 0", "new": "return 1"})),
            response(call("run_tests", {}, "call_2")),
            response(text="Fixed and tested"),
        ]
        with patch("issue_agent.shutil.which", return_value="docker"), patch("issue_agent.subprocess.run") as run:
            run.return_value = SimpleNamespace(returncode=0)
            report = run_agent(client, self.workspace, "Return one")
            docker_command = run.call_args_list[0].args[0]
        self.assertTrue(report["verified"])
        self.assertIn("--network=none", docker_command)
        self.assertIn("--read-only", docker_command)
        self.assertTrue(any("readonly" in arg for arg in docker_command))
        self.assertIn("--entrypoint=python", docker_command)
        self.assertIn("--workdir=/workspace", docker_command)
        self.assertEqual(docker_command[-6:], ["test-image", "-m", "pytest", "-q", "-p", "no:cacheprovider"])

    def test_test_failures_clear_verification_and_are_recorded(self):
        for exit_code in (1, 5, 125):
            with self.subTest(exit_code=exit_code):
                self.workspace.tests_passed = True
                with patch("issue_agent.shutil.which", return_value="docker"), patch(
                        "issue_agent.subprocess.run", return_value=SimpleNamespace(returncode=exit_code)):
                    result = self.workspace.run_tests()
                self.assertEqual(result["exit_code"], exit_code)
                self.assertFalse(self.workspace.tests_passed)
                self.assertEqual(self.workspace.test_runs[-1]["exit_code"], exit_code)

    def test_docker_timeout_and_launch_error_cleanup(self):
        for error in (subprocess.TimeoutExpired("docker", 45), OSError("private details")):
            with self.subTest(error=type(error).__name__):
                with patch("issue_agent.shutil.which", return_value="docker"), patch(
                        "issue_agent.subprocess.run", side_effect=[error, SimpleNamespace(returncode=0)]) as run:
                    result = self.workspace.run_tests()
                self.assertIn("error", result)
                self.assertNotIn("private details", result["error"])
                self.assertFalse(self.workspace.tests_passed)
                container = run.call_args_list[0].args[0][5]
                self.assertEqual(run.call_args_list[1].args[0], ["docker", "rm", "-f", container])

    def test_report_and_events_capture_before_and_after_tests(self):
        client = Mock()
        client.responses.create.side_effect = [
            response(call("run_tests", {}, "baseline")),
            response(call("replace_text", {"path": "app.py", "old": "return 0", "new": "return 1"}, "edit")),
            response(call("run_tests", {}, "verify")),
            response(text="Fixed"),
        ]
        events = io.StringIO()
        with patch("issue_agent.shutil.which", return_value="docker"), patch(
                "issue_agent.subprocess.run", side_effect=[
                    SimpleNamespace(returncode=1), SimpleNamespace(returncode=0),
                    SimpleNamespace(returncode=0), SimpleNamespace(returncode=0)]):
            report = run_agent(client, self.workspace, "Fix answer", event_file=events)
        self.assertTrue(report["verified"])
        self.assertEqual(report["model"], DEFAULT_MODEL)
        self.assertEqual([(r["edit_count"], r["exit_code"]) for r in report["test_runs"]], [(0, 1), (1, 0)])
        self.assertGreaterEqual(report["duration_seconds"], 0)
        entries = [json.loads(line) for line in events.getvalue().splitlines()]
        self.assertEqual([e["call_id"] for e in entries], ["baseline", "edit", "verify"])
        self.assertEqual([e["step"] for e in entries], [1, 2, 3])
        self.assertEqual(json.loads(entries[1]["result"])["edits_remaining"], 2)

    def test_invalid_agent_inputs_never_call_api(self):
        client = Mock()
        for kwargs in ({"max_steps": 0}, {"token_budget": -1}, {"max_output_tokens": True}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                run_agent(client, self.workspace, "Fix", **kwargs)
        with self.assertRaises(ValueError):
            run_agent(client, self.workspace, " \n ")
        client.responses.create.assert_not_called()

    def test_cli_rejects_empty_issue_before_external_checks(self):
        with patch("sys.argv", ["issue_agent.py", str(self.root), "--issue", "   "]), patch(
                "sys.stderr", new_callable=io.StringIO) as stderr, patch("issue_agent.subprocess.run") as run:
            with self.assertRaises(SystemExit) as raised:
                main()
        self.assertEqual(raised.exception.code, 2)
        self.assertIn("--issue must not be empty", stderr.getvalue())
        run.assert_not_called()

    def test_edit_limit_blocks_fourth_change(self):
        (self.root / "app.py").write_text("a = 0\nb = 0\nc = 0\nd = 0\n", encoding="utf-8")
        workspace = Workspace(self.root, "test-image", max_edits=3)

        r1 = workspace.replace_text("app.py", "a = 0", "a = 1")
        r2 = workspace.replace_text("app.py", "b = 0", "b = 1")
        r3 = workspace.replace_text("app.py", "c = 0", "c = 1")
        self.assertNotIn("error", r1)
        self.assertNotIn("error", r2)
        self.assertNotIn("error", r3)
        self.assertEqual(workspace.edit_count, 3)

        with self.assertRaisesRegex(ValueError, "Edit limit"):
            workspace.replace_text("app.py", "d = 0", "d = 1")
        # 第 4 次不应真正写入
        self.assertIn("d = 0", (self.root / "app.py").read_text(encoding="utf-8"))
        self.assertEqual(workspace.edit_count, 3)

    def test_failed_edit_does_not_consume_limit(self):
        # 第一次失败：old 不唯一
        (self.root / "app.py").write_text("x = 0\nx = 0\n", encoding="utf-8")
        workspace = Workspace(self.root, "test-image", max_edits=1)
        with self.assertRaises(ValueError):
            workspace.replace_text("app.py", "x = 0", "x = 1")
        self.assertEqual(workspace.edit_count, 0)

        # 使用同一个 workspace，仍然可以成功改一次。
        ok = workspace.replace_text(
            "app.py", "x = 0\nx = 0\n", "x = 1\nx = 0\n")
        self.assertNotIn("error", ok)
        self.assertEqual(workspace.edit_count, 1)

if __name__ == "__main__":
    unittest.main()
