"""The release workflow's publish step, run with a stand-in `gh` that records what it would upload."""

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(shutil.which("bash") is None or os.name == "nt", reason="needs a POSIX bash")


def publish_script() -> str:
    jobs = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())["jobs"]
    [step] = [s for s in jobs["release"]["steps"] if s.get("name") == "Publish the release"]
    return step["run"]


def published(tmp_path: Path, files: list[str]) -> list[str]:
    (tmp_path / "dist").mkdir()
    for name in files:
        (tmp_path / "dist" / name).write_bytes(b"x")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$GH_ARGS"\n')
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "GH_ARGS": str(tmp_path / "args"),
           "GITHUB_REF_NAME": "v0.3.0"}
    subprocess.run(["bash", "-e", "-c", publish_script()], cwd=tmp_path, env=env, check=True)
    return [a for a in (tmp_path / "args").read_text().splitlines() if a.startswith("dist/")]


RELEASE = ["OpenMyMemo-0.3.0-arm64.dmg", "OpenMyMemo-0.3.0-x86_64.dmg", "OpenMyMemo-0.3.0-x64-setup.exe",
           "openmymemo-0.3.0.mcpb", "openmymemo-0.3.0-py3-none-any.whl", "openmymemo-0.3.0.tar.gz"]


def test_without_the_updater_key_only_the_release_files_are_uploaded(tmp_path):
    assert sorted(published(tmp_path, RELEASE)) == sorted(f"dist/{n}" for n in RELEASE)


def test_with_the_updater_key_the_update_files_and_manifest_are_uploaded_too(tmp_path):
    signed = ["OpenMyMemo-0.3.0-arm64.app.tar.gz", "OpenMyMemo-0.3.0-x86_64.app.tar.gz", "latest.json"]
    uploaded = published(tmp_path, [*RELEASE, *signed, "OpenMyMemo-0.3.0-arm64.app.tar.gz.sig"])
    assert sorted(uploaded) == sorted(f"dist/{n}" for n in [*RELEASE, *signed]), "no .sig: they're in latest.json"
