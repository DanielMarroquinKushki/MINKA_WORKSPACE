"""Tests de core/tools/analysis/freshness.py con repos git reales en tmp_path."""

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cincha.core.tools.analysis.freshness import (
    STATUS_FRESH,
    STATUS_STALE,
    STATUS_UNKNOWN,
    check_freshness,
    format_report,
    load_manifest,
    stale_docs,
)


def _git(repo: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "HOME": str(repo.parent),
    }
    res = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True, env=env
    )
    return res.stdout.strip()


def _commit(repo: Path, rel: str, content: str, msg: str) -> str:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "--short=12", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "usrv-demo"
    r.mkdir()
    _git(r, "init", "-q")
    _commit(r, "gateway/sender.go", "package gateway\n", "base")
    return r


def _manifest(tmp_path: Path, commit: str, paths: list[str], repo_key: str = "demo") -> Path:
    data = {
        "schema_version": 1,
        "docs": {
            "extracted/architecture.md": {
                "extracted_at": "2026-01-01",
                "sources": {repo_key: {"commit": commit, "paths": paths}},
            }
        },
    }
    path = tmp_path / "_sources.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _config(repo: Path) -> SimpleNamespace:
    return SimpleNamespace(
        REPOS={"demo": str(repo)}, ACTIVE_PROJECT="demo", PROJECTS_DIR=repo.parent / "projects"
    )


def test_fresh_when_nothing_changed(repo, tmp_path):
    head = _git(repo, "rev-parse", "--short=12", "HEAD")
    results = check_freshness(_config(repo), manifest=_manifest(tmp_path, head, ["gateway/"]))
    assert [r.status for r in results] == [STATUS_FRESH]
    assert not stale_docs(results)


def test_stale_when_declared_path_changed(repo, tmp_path):
    base = _git(repo, "rev-parse", "--short=12", "HEAD")
    _commit(repo, "gateway/sender.go", "package gateway\n// cambio\n", "toca gateway")
    results = check_freshness(_config(repo), manifest=_manifest(tmp_path, base, ["gateway/"]))
    assert results[0].status == STATUS_STALE
    assert results[0].changed == {"demo": ["gateway/sender.go"]}
    assert "gateway/sender.go" in format_report(results)


def test_fresh_when_change_is_outside_declared_paths(repo, tmp_path):
    base = _git(repo, "rev-parse", "--short=12", "HEAD")
    _commit(repo, "docs/readme.md", "x\n", "toca docs")
    results = check_freshness(_config(repo), manifest=_manifest(tmp_path, base, ["gateway/"]))
    assert results[0].status == STATUS_FRESH


def test_vendor_is_always_excluded(repo, tmp_path):
    base = _git(repo, "rev-parse", "--short=12", "HEAD")
    _commit(repo, "vendor/x/y.go", "package y\n", "vendor")
    results = check_freshness(_config(repo), manifest=_manifest(tmp_path, base, ["."]))
    assert results[0].status == STATUS_FRESH


def test_unknown_when_commit_missing_locally(repo, tmp_path):
    results = check_freshness(_config(repo), manifest=_manifest(tmp_path, "deadbeef", ["."]))
    assert results[0].status == STATUS_UNKNOWN
    assert any("deadbeef" in n for n in results[0].notes)


def test_unknown_when_repo_not_configured(repo, tmp_path):
    head = _git(repo, "rev-parse", "--short=12", "HEAD")
    manifest = _manifest(tmp_path, head, ["."], repo_key="otro")
    results = check_freshness(_config(repo), manifest=manifest)
    assert results[0].status == STATUS_UNKNOWN
    assert any("config.REPOS" in n for n in results[0].notes)


def test_missing_manifest_means_no_docs(tmp_path):
    assert load_manifest(tmp_path / "no-existe.json") == {}
    cfg = SimpleNamespace(REPOS={}, ACTIVE_PROJECT="demo", PROJECTS_DIR=tmp_path / "projects")
    assert check_freshness(cfg) == []


def test_invalid_schema_version_raises(tmp_path):
    path = tmp_path / "_sources.json"
    path.write_text(json.dumps({"schema_version": 99, "docs": {}}), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        load_manifest(path)
