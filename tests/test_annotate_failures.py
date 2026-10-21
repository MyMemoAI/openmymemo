"""scripts/annotate_failures.py: the Windows CI job's failing tests as GitHub annotations."""

import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "annotate_failures.py"


def run(tmp_path, xml: str | None) -> subprocess.CompletedProcess:
    report = tmp_path / "report.xml"
    if xml is not None:
        tmp_path.mkdir(exist_ok=True)
        report.write_text(xml, encoding="utf-8")
    return subprocess.run([sys.executable, str(SCRIPT), str(report)], capture_output=True, text=True)


def test_each_failure_and_error_becomes_an_annotation(tmp_path):
    r = run(tmp_path, """<testsuites><testsuite>
      <testcase classname="tests.test_a" name="test_ok"/>
      <testcase classname="tests.test_a" name="test_bad"><failure message="assert 1 == 2">line one
line 100%</failure></testcase>
      <testcase classname="tests.test_b" name="test_setup"><error message="fixture broke"/></testcase>
    </testsuite></testsuites>""")
    lines = r.stdout.splitlines()
    assert r.returncode == 1 and len(lines) == 2
    assert lines[0] == "::error title=tests.test_a::test_bad::assert 1 == 2%0Aline one%0Aline 100%25"
    assert lines[1].startswith("::error title=tests.test_b::test_setup::fixture broke")


def test_a_report_without_failures_or_without_a_report(tmp_path):
    # An interrupted run or one that collected nothing has no failures to show; CI keeps pytest's own status.
    assert run(tmp_path, "<testsuites><testsuite/></testsuites>").returncode == 0
    r = run(tmp_path / "missing", None)
    assert r.returncode == 1 and r.stdout.startswith("::error::No test report")
