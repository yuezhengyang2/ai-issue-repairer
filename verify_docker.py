"""Verify real Docker execution without calling an AI API (takes about a minute)."""

import argparse
import json
from pathlib import Path
import subprocess
import uuid
from unittest.mock import patch

from issue_agent import Workspace, snapshot


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="issue-agent-tests:local")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent
    output = project / "runs" / ("docker-check-" + uuid.uuid4().hex[:12])
    output.mkdir(parents=True)
    report = {"image": args.image, "api_calls": 0, "checks": {}, "test_runs": []}
    containers = []
    real_run = subprocess.run

    def record_run(command, **kwargs):
        # Observe container names; every subprocess still executes for real.
        if command[:2] == ["docker", "run"]:
            containers.append(command[command.index("--name") + 1])
        return real_run(command, **kwargs)

    def test(workspace, label):
        print(f"Running {label}...", flush=True)
        with patch("issue_agent.subprocess.run", side_effect=record_run):
            result = workspace.run_tests()
        report["test_runs"].append({"label": label, **workspace.test_runs[-1]})
        print(json.dumps(result, ensure_ascii=False), flush=True)
        return result

    try:
        source = project / "examples" / "buggy_repo"
        originals = {p.name: p.read_bytes() for p in source.iterdir() if p.is_file()}
        snapshot(source, output / "workspace")
        workspace = Workspace(output / "workspace", args.image)
        baseline = test(workspace, "baseline")
        report["checks"]["bug_reproduced"] = (
            baseline.get("exit_code") == 1 and "1 failed, 1 passed" in baseline.get("output", ""))
        if not report["checks"]["bug_reproduced"]:
            raise RuntimeError("Expected the example bug to fail one test before editing.")
        workspace.replace_text("date_utils.py", "    return date.fromisoformat(value)",
                               '    if value == "":\n        return None\n    return date.fromisoformat(value)')
        fixed = test(workspace, "after_edit")
        report["checks"]["repair_passed"] = (
            fixed.get("exit_code") == 0 and "2 passed" in fixed.get("output", ""))
        diff = workspace.patch()
        (output / "fix.patch").write_bytes(diff.encode("utf-8"))
        report["checks"]["source_unchanged"] = all(
            (source / name).read_bytes() == data for name, data in originals.items())
        report["checks"]["tests_unchanged"] = (
            (workspace.root / "test_date_utils.py").read_bytes() == originals["test_date_utils.py"])
        report["checks"]["source_only_patch"] = (
            "+++ b/date_utils.py" in diff and "test_date_utils.py" not in diff)

        empty = output / "no_tests"
        empty.mkdir()
        (empty / "app.py").write_text("value = 1\n", encoding="utf-8")
        empty_workspace = Workspace(empty, args.image)
        no_tests = test(empty_workspace, "no_tests")
        report["checks"]["empty_suite_rejected"] = (
            no_tests.get("exit_code") == 5 and not empty_workspace.tests_passed)

        slow = output / "timeout"
        slow.mkdir()
        (slow / "test_slow.py").write_text(
            "import time\n\ndef test_slow():\n    time.sleep(120)\n", encoding="utf-8")
        slow_workspace = Workspace(slow, args.image)
        timed_out = test(slow_workspace, "timeout (45 seconds)")
        report["checks"]["timeout_rejected"] = (
            "45-second timeout" in timed_out.get("error", "") and not slow_workspace.tests_passed)
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        # Check only this verifier's containers; leave other Docker workloads alone.
        try:
            listing = real_run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                               check=True, capture_output=True, text=True, timeout=10)
            remaining = set(listing.stdout.splitlines()).intersection(containers)
            report["checks"]["containers_removed"] = bool(containers) and not remaining
            report["remaining_containers"] = sorted(remaining)
        except (OSError, subprocess.SubprocessError) as exc:
            report["checks"]["containers_removed"] = False
            report["cleanup_check_error"] = type(exc).__name__
        report["passed"] = "error" not in report and all(report["checks"].values())
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Report: {output / 'report.json'}", flush=True)
        print(f"Passed: {report['passed']}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
