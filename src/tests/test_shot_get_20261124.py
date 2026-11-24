"""★2026-11-24 Android から会話の写真を取りに来る操作 shot.get（remotectl.py）と、Chat モードの Image・作業場所。

オーナー「Android の方も頼みます。特にモデルがチャット欄とかに画像を表示してくれる機能とかについてより優れたものに」。
写真は `<home>/sessions/<会話>/shots/` にあり、道具の要約と答えの `![]()` にこの機械の絶対パスで載る。
"""
from __future__ import annotations

import base64
import os
import types

import pytest

from laps import remotectl
from laps.engine import prompt, server
from laps.engine.tools import Ctx, base_tools, image

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _gw(tmp_path):
    home = tmp_path / "home"
    (home / "sessions" / "s1" / "shots").mkdir(parents=True)
    return types.SimpleNamespace(home=str(home)), home


def test_reads_a_conversation_shot(tmp_path):
    gw, home = _gw(tmp_path)
    f = home / "sessions" / "s1" / "shots" / "img-abc.png"
    f.write_bytes(PNG)
    r = remotectl.shot_get(gw, {"path": str(f)})
    assert r["mime"] == "image/png" and r["size"] == len(PNG) and r["done"] is True
    assert base64.b64decode(r["data"]) == PNG


def test_chunks_join_back(tmp_path, monkeypatch):
    gw, home = _gw(tmp_path)
    body = b"\xff\xd8\xff" + os.urandom(10_000)
    f = home / "sessions" / "s1" / "shots" / "c1.jpg"
    f.write_bytes(body)
    monkeypatch.setattr(remotectl, "SHOT_CHUNK", 4096)
    got, off = b"", 0
    for _ in range(10):
        r = remotectl.shot_get(gw, {"path": str(f), "offset": off})
        assert r["offset"] == off
        got += base64.b64decode(r["data"])
        off += len(base64.b64decode(r["data"]))
        if r["done"]:
            break
    assert got == body and r["mime"] == "image/jpeg"


@pytest.mark.parametrize("where", [
    "outside",            # home の外
    "notshots",           # 会話のフォルダの直下（shots ではない）
    "deep",               # shots の下のさらに下
    "othershots",         # home の直下の shots（会話のフォルダではない）
])
def test_only_conversation_shots(tmp_path, where):
    gw, home = _gw(tmp_path)
    p = {"outside": tmp_path / "shots" / "a.png",
         "notshots": home / "sessions" / "s1" / "a.png",
         "deep": home / "sessions" / "s1" / "shots" / "x" / "a.png",
         "othershots": home / "shots" / "a.png"}[where]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(PNG)
    with pytest.raises(remotectl.CtlError) as e:
        remotectl.shot_get(gw, {"path": str(p)})
    assert e.value.code == "denied"


def test_dotdot_and_symlink_out(tmp_path):
    gw, home = _gw(tmp_path)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG)
    with pytest.raises(remotectl.CtlError):
        remotectl.shot_get(gw, {"path": str(home / "sessions" / "s1" / "shots" / ".." / ".." / ".." / ".." / "secret.png")})
    link = home / "sessions" / "s1" / "shots" / "l.png"
    try:
        link.symlink_to(secret)
    except OSError:
        pytest.skip("symlink not available")
    with pytest.raises(remotectl.CtlError) as e:
        remotectl.shot_get(gw, {"path": str(link)})       # 実体は home の外
    assert e.value.code == "denied"


def test_swapped_between_check_and_open_is_refused(tmp_path, monkeypatch):
    """確かめた後・開く前に、同じ名前を home の外の物へのシンボリックリンクに差し替えても読ませない（O_NOFOLLOW と st_ino の突き合わせ）。"""
    gw, home = _gw(tmp_path)
    f = home / "sessions" / "s1" / "shots" / "a.png"
    f.write_bytes(PNG)
    secret = tmp_path / "secret.png"
    secret.write_bytes(PNG + b"SECRET")
    real_open = remotectl.os.open

    def swap_then_open(path, flags, *a, **k):
        f.unlink()
        try:
            f.symlink_to(secret)
        except OSError:
            f.write_bytes(PNG + b"other")           # リンクを作れない所では別の中身に差し替える（st_ino が変わる）
        return real_open(path, flags, *a, **k)

    monkeypatch.setattr(remotectl.os, "open", swap_then_open)
    with pytest.raises(remotectl.CtlError):
        remotectl.shot_get(gw, {"path": str(f)})


def test_extension_and_content_must_be_an_image(tmp_path):
    gw, home = _gw(tmp_path)
    d = home / "sessions" / "s1" / "shots"
    (d / "a.txt").write_bytes(PNG)
    (d / "b.PNG").write_bytes(b"not an image at all")
    (d / "c.JPG").write_bytes(b"\xff\xd8\xff" + b"x" * 10)
    with pytest.raises(remotectl.CtlError):
        remotectl.shot_get(gw, {"path": str(d / "a.txt")})
    with pytest.raises(remotectl.CtlError) as e:
        remotectl.shot_get(gw, {"path": str(d / "b.PNG")})
    assert "画像ではありません" in e.value.message
    assert remotectl.shot_get(gw, {"path": str(d / "c.JPG")})["mime"] == "image/jpeg"   # 大文字の拡張子


def test_size_limit_and_missing(tmp_path, monkeypatch):
    gw, home = _gw(tmp_path)
    f = home / "sessions" / "s1" / "shots" / "big.png"
    f.write_bytes(PNG + b"\x00" * 200)
    monkeypatch.setattr(remotectl, "SHOT_MAX", 100)
    with pytest.raises(remotectl.CtlError) as e:
        remotectl.shot_get(gw, {"path": str(f)})
    assert e.value.code == "too_large"
    with pytest.raises(remotectl.CtlError) as e:
        remotectl.shot_get(gw, {"path": str(f.with_name("nope.png"))})
    assert e.value.code == "not_found"
    with pytest.raises(remotectl.CtlError):
        remotectl.shot_get(gw, {"path": ""})


def test_shot_get_is_a_native_op():
    assert remotectl.NATIVE["shot.get"] is remotectl.shot_get


# ── Chat モード（talk）────────────────────────────────────────────

def test_talk_has_web_image_but_no_file_tools():
    tools = base_tools("talk")
    assert set(tools) == {"WebSearch", "WebFetch", "Image"}
    assert tools["Image"] is image.WEB_IMAGE
    assert "url" in tools["Image"].params and "path" not in tools["Image"].params
    assert "Image" in prompt.TALK


def test_talk_image_refuses_local_files(tmp_path):
    f = tmp_path / "a.png"
    f.write_bytes(PNG)
    import threading
    ctx = Ctx(workspace=str(tmp_path), root=tmp_path, cancel=threading.Event(), preempt=threading.Event())
    r = image.web_show({"path": str(f)}, ctx)
    assert not r.ok and r.kind == "denied"
    assert not (tmp_path / "shots").exists()


def test_talk_without_workspace_runs_in_home_chat(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "h"))
    monkeypatch.setenv("LAPS_SERVE_WORKSPACE_ROOT", str(tmp_path / "allowed"))
    spec = server.parse_run({"id": "r1", "prompt": "こんにちは", "tools": "talk"}, 1)
    assert spec.toolset == "talk"
    assert spec.workspace == str((tmp_path / "h" / "chat").resolve())
    # talk 以外は今までどおり許可根で断る（talk と名乗らない限り検査は外れない）
    with pytest.raises(server.Rejected):
        server.parse_run({"id": "r2", "prompt": "x", "tools": "full", "workspace": str(tmp_path / "else")}, 2)
    # talk でも作業フォルダーを名指しすれば、許可根の検査は残る
    with pytest.raises(server.Rejected):
        server.parse_run({"id": "r3", "prompt": "x", "tools": "talk", "workspace": str(tmp_path / "else")}, 3)
