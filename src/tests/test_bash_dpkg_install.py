"""`sudo dpkg -i` の導線（第3回 W2・2026-09-04）。

★背景（`REPORTS/vdir-full-fidelity-20260824.md` より）: VDIR 走行では
  「/srv/packages から dpkg で自力整備」が実際に検収を通した戦略で、Bash が
  この経路を正しく通せるかは実務に直結する。同じ記録には `dpkg -i` した
  パッケージが基底パッケージと衝突して apt resolver を壊した事故も残っている
  ＝dpkg -i の**成功/失敗どちらの戻りも**エージェントが正しく読める必要がある。

★安全のための設計: ここで作る .deb は中身が `/tmp/<一意な名前>/marker.txt` だけの
  空パッケージで、postinst/prerm は持たない（システムへの副作用は一切無い）。
  package 名も乱数サフィックス付きで、実在の `blackwall` パッケージ等と衝突しない。
  試験の最後に必ず `dpkg --purge` で片付ける（失敗しても teardown で片付く）。

  policy=trusted のときだけ sudo が通る設計（`policy.py` の `_ALWAYS_DENIED`）。
  readonly/workspace では dpkg は常に拒否されることも確かめる。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid

import pytest

from laps.tools import bash as bash_tool

DPKG_DEB = shutil.which("dpkg-deb")
DPKG = shutil.which("dpkg")


def _text(out) -> str:
    return out if isinstance(out, str) else out.text


def _purge_quiet(pkg_name: str) -> None:
    subprocess.run(["dpkg", "--purge", pkg_name], capture_output=True, text=True)


@pytest.fixture(autouse=True)
def _trusted(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")


@pytest.mark.skipif(not DPKG_DEB or not DPKG, reason="dpkg-deb/dpkg が無い")
@pytest.mark.skipif(os.geteuid() != 0, reason="dpkg -i の実インストールには root が要る")
def test_自作debのdpkg_iが実際に通り本文にokと出る(tmp_path):
    pkg = f"bw-w2-dummy-{uuid.uuid4().hex[:8]}"
    marker_dir = f"bw_w2_marker_{uuid.uuid4().hex[:8]}"
    root = tmp_path / "root"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "tmp" / marker_dir).mkdir(parents=True)
    marker_abs = f"/tmp/{marker_dir}/marker.txt"
    (root / "tmp" / marker_dir / "marker.txt").write_text("安全なマーカーです\n", encoding="utf-8")
    (root / "DEBIAN" / "control").write_text(
        f"Package: {pkg}\nVersion: 1.0\nSection: misc\nPriority: optional\n"
        "Architecture: amd64\nMaintainer: blackwall-w2-test <noreply@example.invalid>\n"
        "Description: harmless dummy package for Bash dpkg -i test (W2)\n"
        " Installs one marker file under /tmp only. Safe to purge.\n",
        encoding="utf-8")
    for d in root.rglob("*"):
        d.chmod(0o755 if d.is_dir() else 0o644)
    deb = tmp_path / f"{pkg}_1.0_amd64.deb"
    r = subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    try:
        out = bash_tool._run({"command": f"sudo dpkg -i {deb}"})
        assert out.ok is True, out
        assert out.kind == "ok", out.kind
        assert "[exit=" not in out, "成功しているのに exit タグが付いている"
        # 実際にシステムへ入ったことを、道具の外側から確かめる
        assert os.path.exists(marker_abs), "パッケージの中身が実際には展開されていない"
        listed = subprocess.run(["dpkg", "-l", pkg], capture_output=True, text=True)
        assert f"ii  {pkg}" in listed.stdout or pkg in listed.stdout
    finally:
        _purge_quiet(pkg)
        assert not os.path.exists(marker_abs), "purge してもマーカーが残っている（後片付け漏れ）"


@pytest.mark.skipif(not DPKG_DEB or not DPKG, reason="dpkg-deb/dpkg が無い")
@pytest.mark.skipif(os.geteuid() != 0, reason="dpkg -i の実インストールには root が要る")
def test_壊れたdebのdpkg_iは失敗としてokFalseで返りシステムを汚さない(tmp_path):
    """アーカイブとして壊れている .deb（ar 形式ですらない）を渡す。
    dpkg-deb がその場で拒否するので、パッケージ DB には何も残らない。"""
    bad = tmp_path / "garbage.deb"
    bad.write_bytes(os.urandom(500))
    pkg_guess = "garbage"  # 何も登録されないはずなので、念のため確かめるだけ

    out = bash_tool._run({"command": f"sudo dpkg -i {bad}"})
    assert out.ok is False, out
    assert out.kind == "error", out.kind
    assert "[exit=" in out, "失敗の終了コードが本文に無い"
    assert "�" not in out, "エラーメッセージが文字化けしている"
    # dpkg 自身の言葉で「Debian 形式のアーカイブでない」と読める
    assert ("not a Debian format archive" in out or "Debian" in out), out

    listed = subprocess.run(["dpkg", "-l"], capture_output=True, text=True)
    assert pkg_guess not in listed.stdout, "壊れたアーカイブなのにパッケージ DB へ登録されている"


@pytest.mark.skipif(not DPKG_DEB or not DPKG, reason="dpkg-deb/dpkg が無い")
@pytest.mark.skipif(os.geteuid() != 0, reason="dpkg -i の実インストールには root が要る")
def test_満たせない依存を持つdebは設定失敗として正直に返る(tmp_path):
    """有効な deb だが依存が満たせない場合。dpkg は「展開はしたが設定できない」
    という exit!=0 を返す——`dpkg -i` の失敗形の中でも壊れたアーカイブとは別の型。"""
    pkg = f"bw-w2-baddep-{uuid.uuid4().hex[:8]}"
    marker_dir = f"bw_w2_baddep_{uuid.uuid4().hex[:8]}"
    root = tmp_path / "root"
    (root / "DEBIAN").mkdir(parents=True)
    (root / "tmp" / marker_dir).mkdir(parents=True)
    (root / "tmp" / marker_dir / "marker.txt").write_text("x\n", encoding="utf-8")
    (root / "DEBIAN" / "control").write_text(
        f"Package: {pkg}\nVersion: 1.0\nSection: misc\nPriority: optional\n"
        "Architecture: amd64\nMaintainer: blackwall-w2-test <noreply@example.invalid>\n"
        f"Depends: bw-w2-nonexistent-dep-{uuid.uuid4().hex[:8]}\n"
        "Description: harmless dummy package with an unsatisfiable dep (W2)\n"
        " Used only to exercise the failure path of dpkg -i.\n",
        encoding="utf-8")
    for d in root.rglob("*"):
        d.chmod(0o755 if d.is_dir() else 0o644)
    deb = tmp_path / f"{pkg}_1.0_amd64.deb"
    r = subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(deb)],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr

    try:
        out = bash_tool._run({"command": f"sudo dpkg -i {deb}"})
        assert out.ok is False, out
        assert out.kind == "error", out.kind
        assert "dependency" in out.lower() or "依存" in out, out
    finally:
        # unconfigured のまま残るので --purge --force-depends で確実に片付ける
        subprocess.run(["dpkg", "--purge", "--force-depends", pkg],
                       capture_output=True, text=True)


@pytest.mark.parametrize("mode", ["readonly", "workspace"])
def test_readonlyとworkspaceではdpkgは常に拒否される(monkeypatch, mode):
    """`_ALWAYS_DENIED` に dpkg/sudo が入っている——trusted 以外では実行させない。
    ここは中身の deb が本物かどうかに関係なく、door を叩く前で止まることを見る。"""
    monkeypatch.setenv("LAPS_POLICY", mode)
    out = bash_tool._run({"command": "sudo dpkg -i /tmp/does-not-matter.deb"})
    assert "policy denied" in str(out)
    assert not str(out).startswith("OK")
