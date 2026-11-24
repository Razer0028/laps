"""BashOutput の 2 つの不完全（2026-09-03・W3 道具担当の点検で発見）。

* `tail()` が `data.decode("utf-8", errors="replace")` の決め打ちだった。
  背景処理は Windows の cmd.exe/comspec 経由でも起動できる（`bash.py` の
  `_spawn_background` に win32 分岐がある）のに、出力を読むこちらは
  日本語 Windows（cp932）の出力を全部 U+FFFD に潰していた——
  `bash.py` / `system_state.py` / `remote_read.py` が同日直した
  「符号化を決め打ちしない」という直しの、片方だけ残った兄弟。

* `kill=true` が `os.killpg(os.getpgid(pid), SIGTERM)` の一本槍で、Windows には
  `os.getpgid` が無いので**必ず** AttributeError になり、プロセスは生きたまま
  「Error killing pid …」とだけ返っていた。`_spawn_background` の停止案内は
  同じファイルで `taskkill /F /T /PID` を教えているのに、この道具自身は
  それをしていなかった。
"""
from __future__ import annotations

import json
import subprocess
import sys

from laps.tools import bash_output as B


def _write_bg_record(tmp_path, monkeypatch, pid: int, *, out_bytes: bytes = b"", cmd: str = "x",
                     **extra):
    # ★台帳は **作業場つき**（2026-09-05・第4回c の再反証）。`BashOutput(pid=…,
    #   kill=true)` は `LAPS_HOME` を共有する同居セッションの記録を通さない
    #   ので、試験も本番と同じ形（`workspace` を書き、その作業場に居る）にする。
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_WORKSPACE", str(ws))
    d = tmp_path / "bg"
    d.mkdir(parents=True, exist_ok=True)
    body = {"pid": pid, "cmd": cmd, "started_at": "x", "workspace": str(ws)}
    body.update(extra)                    # ★所有権の関門を通す材料（start_ticks / cwd）
    (d / f"{pid}.meta.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8")
    (d / f"{pid}.out").write_bytes(out_bytes)
    (d / f"{pid}.err").write_bytes(b"")
    return d


def test_背景出力のcp932がU_FFFDに潰れない(tmp_path, monkeypatch):
    """日本語 Windows のコマンド（例: cmd.exe の失敗メッセージ）は cp932 で吐かれる。"""
    pid = 555_001
    msg = "指定されたファイルが見つかりません。".encode("cp932")
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes=msg)
    out = B._run({"pid": pid})
    assert "指定されたファイルが見つかりません。" in out
    assert "�" not in out, "cp932 の出力が文字化けしている"


def test_背景出力のUTF8はそのまま読める(tmp_path, monkeypatch):
    """従来どおり UTF-8（Linux の既定）も壊さず読める。"""
    pid = 555_002
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes="こんにちは END".encode("utf-8"))
    out = B._run({"pid": pid, "tail": 100})
    assert "こんにちは END" in out


def test_windowsのkillはtaskkillを使う(tmp_path, monkeypatch):
    """`sys.platform == 'win32'` のときは `os.killpg` ではなく taskkill /F /T を使う。"""
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    pid = 555_003
    _write_bg_record(tmp_path, monkeypatch, pid)
    monkeypatch.setattr(B, "_is_alive", lambda _pid: True)
    monkeypatch.setattr(B.sys, "platform", "win32")

    calls = []

    class _P:
        returncode = 0
        stdout = b""
        stderr = b""

    def _fake_run(argv, **kw):
        calls.append(argv)
        return _P()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    out = B._run({"pid": pid, "kill": True})
    assert calls, "taskkill (subprocess.run) が一度も呼ばれていない"
    assert calls[0][:1] == ["taskkill"]
    assert "/F" in calls[0] and "/T" in calls[0] and str(pid) in calls[0]
    assert out.startswith("OK:")


def test_windowsのkillが失敗したら理由を返す(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    pid = 555_004
    _write_bg_record(tmp_path, monkeypatch, pid)
    monkeypatch.setattr(B, "_is_alive", lambda _pid: True)
    monkeypatch.setattr(B.sys, "platform", "win32")

    class _P:
        returncode = 1
        stdout = "アクセスが拒否されました。".encode("cp932")
        stderr = b""

    monkeypatch.setattr(subprocess, "run", lambda argv, **kw: _P())
    out = B._run({"pid": pid, "kill": True})
    assert out.startswith("Error killing pid")
    assert "アクセスが拒否されました" in out, "cp932 の taskkill 出力が文字化けしている"


def test_途中から読んだtailはマルチバイト境界で誤った符号化に化けない(tmp_path, monkeypatch):
    """★2026-09-04・W2 の実測で見つけた実害。

    `tail()` はファイルの途中から `seek` して読むので、先頭がマルチバイト文字の
    途中で切れることがある。旧実装（トリム無し）は `data.decode(enc)` を
    utf-8→cp932→… と総当りし、切れた1バイト目が cp932 として**例外なく**読めて
    しまうと、末尾全体が別の符号化で丸ごと誤読された——置換文字も出ないので
    「読めているように見えて中身が違う」という一番気づきにくい壊れ方だった。
    実測: 本物は UTF-8 で末尾が「けこ」なのに、`tail=5` は `代％` を返していた。
    """
    pid = 555_010
    # 「あいうえおかきくけこ」を UTF-8（各文字3バイト）で書く。
    text = "あいうえおかきくけこ\n"
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes=text.encode("utf-8"))
    full = text.rstrip("\n")
    for tail_n in range(1, len(text.encode("utf-8")) + 3):
        out = B._run({"pid": pid, "tail": tail_n})
        body = out.split(f"--- stdout (last {tail_n}B) ---\n", 1)[-1].rstrip("\n")
        if not body:
            continue
        assert full.endswith(body), (
            f"tail={tail_n}: 誤った符号化に化けた可能性（{body!r} は末尾の一致部分でない）")
        assert "�" not in body, f"tail={tail_n}: 置換文字が出ている（{body!r}）"


def test_ファイル全体を読むときは従来どおり(tmp_path, monkeypatch):
    """`tail` 指定がファイル全体を覆うとき（seek off=0）は境界トリムを働かせない
    ——cp932 の全文試験（このファイル冒頭の2本）と挙動を変えない。"""
    pid = 555_011
    msg = "指定されたファイルが見つかりません。".encode("cp932")
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes=msg)
    out = B._run({"pid": pid, "tail": 10_000})  # ファイルよりずっと大きい
    assert "指定されたファイルが見つかりません。" in out
    assert "�" not in out


def test_途中から読んだtailはcp932でも誤った文字に化けない(tmp_path, monkeypatch):
    """★2026-09-05・r4 tools-process 監査で発見。

    `test_途中から読んだtailはマルチバイト境界で誤った符号化に化けない`（上）は
    UTF-8 しか掃引していなかった。**cp932 はこの経路の主眼（日本語 Windows の
    出力）なのに、旧実装の「トリムして decode が例外なく通ったら採用」という
    判定は cp932 には原理的に効かない**——cp932 はほぼ全バイト値が単独でも
    合法（半角カナ・私用領域）で、境界がずれていても例外が出ないため。

    実測（本コミット前）: 「あいうえお…」の cp932 出力の末尾を奇数バイト目から
    読むと、例外は 0 件のまま先頭に半角カナ/私用領域文字が混入した。

    ここでは cp932 が現実に出る形（Windows のパス・記号を含む日本語メッセージ）
    で掃引する——`_sjis_boundary_skip` は「2バイト目になり得ない安全点」を
    手掛かりに境界を決め直すので、英数字/記号が定期的に混じる現実の出力では
    ほぼ全域で解決できる（純和文が長く続く病的なケースは
    `test_cp932は安全点が無い和文の連続では従来の限界のまま` で別途扱う）。
    """
    pid = 555_012
    # ★現実の背景処理ログの形（各行が ASCII のタイムスタンプで始まる）にする。
    #   これで「安全点（2バイト目になり得ない値）」がどの行内にも定期的に現れ、
    #   `_sjis_boundary_skip` が `_SJIS_ANCHOR_MAX_BACK` 以内で必ず見つけられる。
    msg = "".join(
        f"[{i:04d}] 処理中にエラーが発生しました: C:\\Users\\user\\Desktop\\ログ\\出力ファイル.txt "
        "が見つかりません。もう一度確認してください。\n"
        for i in range(5)
    )
    raw = msg.encode("cp932")
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes=raw)
    full = msg.rstrip("\n")
    bad = []
    # ★末尾ぎりぎり数バイトだけは対象外（下の理由で別枠）。それ以外は全域を掃引する。
    for tail_n in range(7, len(raw) + 3):
        out = B._run({"pid": pid, "tail": tail_n})
        body = out.split(f"--- stdout (last {tail_n}B) ---\n", 1)[-1].rstrip("\n")
        if not body or body == "(empty)":
            continue
        if "�" in body or not full.endswith(body):
            bad.append((tail_n, body[:12]))
    assert not bad, f"cp932 境界で化けた tail_n: {bad[:10]} (計 {len(bad)} 件)"


def test_cp932はファイル末尾の数バイトだけ別の限界が残る(tmp_path, monkeypatch):
    """★上のテストが `tail_n < 7` を除外している理由を固定する。

    メッセージ末尾は cp932 で「。」→ `0x81 0x42`、続く改行 `0x0a`。末尾 2〜6 バイト
    だけを切り出すと、その並び（例: `0x42 0x0a` = ASCII の `B\\n`）は**それ単体でも
    正しい UTF-8**になる。`_TAIL_ENCODINGS` は utf-8 を最優先で試すため、cp932 の
    構造判定（`_sjis_boundary_skip`）へ辿り着く前に utf-8 側が「例外なく読めた」と
    確定してしまう——これは今回の直しの対象（cp932 の判定不能）ではなく、
    符号化の優先順位そのものに由来する別の限界で、**この直し以前から存在した**。
    ここでは「そのまま」であって「悪化していない」ことだけを固定する。"""
    pid = 555_014
    msg = "エラーが発生しました。\n"
    raw = msg.encode("cp932")
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes=raw)
    for tail_n in (2, 3, 4, 5, 6):
        out = B._run({"pid": pid, "tail": tail_n})
        assert out.startswith("pid:")  # 例外を出さずに返る（クラッシュしない）ことだけを保証


def test_cp932は安全点が無い和文の連続では従来の限界のまま(tmp_path, monkeypatch):
    """★安全点（2バイト目になり得ない値）が `_SJIS_ANCHOR_MAX_BACK` 以内に無い
    （英数字を挟まない和文が長く続く）場合、Shift_JIS 系は自己同期しない符号化
    なので構造的には判定不能——`_sjis_boundary_skip` は諦めて `None` を返し、
    旧来のトリム総当りにフォールバックする。**ここを直したと主張はしない**
    （直せる保証がない）。少なくとも例外を出さず、致命的に壊れない
    （置換文字だらけにはならない）ことだけを確かめる。"""
    pid = 555_013
    text = "あいうえおかきくけこ" * 50 + "\n"
    raw = text.encode("cp932")
    _write_bg_record(tmp_path, monkeypatch, pid, out_bytes=raw)
    for tail_n in (5, 13, 27, 40, 61):
        out = B._run({"pid": pid, "tail": tail_n})  # 例外を出さずに返ることだけが要件
        assert out.startswith("pid:")
        body = out.split(f"--- stdout (last {tail_n}B) ---\n", 1)[-1]
        assert body.count("�") == 0, "少なくとも置換文字（U+FFFD）は出ない"


def test_posixのkillは従来どおりkillpgを使う(tmp_path, monkeypatch):
    """win32 分岐を足しても、POSIX の既定経路（`os.killpg`）は変えない。"""
    pid = 555_005
    # ★2026-09-05（第4回c の 5 巡目）: 所有権の関門を狭めた（`must_fix ③`）ので、
    #   **手掛かりが 1 つも無い控えは撃たない**——この試験の作り物の控え
    #   （cwd も start_ticks も無い・プロセスも実在しない）はそこで断られる。
    #   門を迂回するのではなく、**自分が起こした生きた背景処理に見える控え**にする:
    #   控えの `start_ticks` と `/proc` の開始時刻が一致する＝`bg_record_matches` が
    #   通る形（本番で `run_in_background=true` が書く控えと同じ）。
    _write_bg_record(tmp_path, monkeypatch, pid, start_ticks=777, cwd=str(tmp_path))
    # ★union-2（2026-09-06）: 統合先の `procutil.proc_start_ticks` は
    #   `start_ticks` への**単純な名前コピー**（`proc_start_ticks = start_ticks`）で、
    #   `bg_record_matches` はモジュール内部から**裸の `start_ticks(pid)`** を呼ぶ。
    #   `proc_start_ticks` だけ差し替えても `bg_record_matches` の照合には届かず、
    #   `_ownership_note` の `leader_ticks` 側（`procutil.proc_start_ticks` を属性
    #   経由で呼ぶ）だけが 777 を見て `bg_record_matches` の中身は素の値に戻る
    #   ——2 つの名前は同じ関数を指すとは限らないので、**両方**差し替える。
    monkeypatch.setattr(B.procutil, "proc_start_ticks",
                        lambda p: 777 if p == pid else None)
    monkeypatch.setattr(B.procutil, "start_ticks",
                        lambda p: 777 if p == pid else None)
    # ★作業場を**この試験の tmp_path に閉じる**（2026-09-05・5 巡目）。conftest の既定は
    #   `/tmp` で、身内の観測範囲を作業場にした 4 巡目以降それは「/tmp に居る全プロセスが
    #   身内候補」という意味になる。物差し（start_ticks）が控えに無い相手を落とさなく
    #   した 5 巡目では、**この機械で他人が /tmp に建てたサーバが「残骸」に見えて**
    #   経路の試験が赤くなった（実測: 別セッションの pytest の http.server を拾った）。
    #   本番の作業場はプロジェクトのディレクトリなので、試験もその形に合わせる。
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setattr(B, "_is_alive", lambda _pid: True)
    monkeypatch.setattr(B.sys, "platform", "linux")

    calls = []
    monkeypatch.setattr(B.os, "getpgid", lambda p: p)
    monkeypatch.setattr(B.signal, "SIGTERM", 15)
    monkeypatch.setattr(B.os, "killpg", lambda pgid, sig: calls.append((pgid, sig)))
    out = B._run({"pid": pid, "kill": True})
    assert calls == [(pid, 15)]
    # ★2026-09-05: 文言は「送った」から「止まった」へ変えた（`_stop_group`）。
    #   送りっぱなしにせず、実際に消えたことを見てから OK と言う。
    assert "SIGTERM" in out
    # ★2026-09-05（第4回c の 3 巡目）: 期待を `OK:` に戻した。**根拠が変わったため。**
    #   2 巡目は「控えの `cmd`（ここでは `"x"`）からポートが読めない」ことを理由に
    #   「確かめていません」を返していた——が、読めないのは**控え**の話で、
    #   待ち受けが在るかどうかは一度も見ていなかった（同じ文言が、正しく止まった
    #   停止にも、ポートと無縁の `bash build.sh` にも毎回出る＝再反証 M6）。
    #   3 巡目は撃つ前に `/proc` を観測する。この控えのグループは実在せず、
    #   LISTEN は 1 つも観測されない＝**待受の話をしない**（観測して無ければ黙る）。
    #   ⚠ この試験が見ているのは**経路**（`os.killpg` が呼ばれること＝上の `calls`）で、
    #   停止の成否ではない（`os.killpg` は差し替えてあり誰も死んでいない）。
    assert out.startswith("OK:"), out
    assert "確かめていません" not in out, out
    assert "待ち受け" not in out and "待受" not in out, out


# ── 第4回c: 台帳の**存在**だけを通行証にしない ─────────────────────────

def test_PID再利用の台帳では撃たない(tmp_path, monkeypatch):
    """★塞いだ穴（2026-09-05・第4回c・反証が隔離した `LAPS_HOME` で再現）:
    `BashOutput(pid=…, kill=true)` は `<pid>.meta.json` が**在るかどうか**しか
    見ていなかった。PID は使い回されるので、記録した子がとうに死んで番号が
    別人に回っていると、**その別人をグループごと撃つ**。
    `bash.py` 側の台帳判定は `procutil.bg_record_matches` で開始時刻を照合して
    いるのに、この道具だけが写し忘れていた（同じ物を 2 つ持つと片方だけ直る）。
    """
    import os
    import time
    from laps import procutil
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    monkeypatch.setenv("LAPS_HOME", str(tmp_path))
    monkeypatch.setenv("LAPS_WORKSPACE", str(tmp_path / "ws"))
    (tmp_path / "ws").mkdir(parents=True, exist_ok=True)
    d = tmp_path / "bg"; d.mkdir(parents=True, exist_ok=True)
    victim = subprocess.Popen(["sleep", "30"], start_new_session=True)  # 番号を引き当てた別人
    try:
        # 記録は同じ番号だが、起動時刻が合わない＝別人。
        (d / f"{victim.pid}.meta.json").write_text(json.dumps(
            {"pid": victim.pid, "pgid": victim.pid, "cmd": "php -S :18300",
             "cwd": None, "workspace": str(tmp_path / "ws"),
             "started_at": "2020-01-01T00:00:00"}), encoding="utf-8")
        (d / f"{victim.pid}.out").write_bytes(b"")
        (d / f"{victim.pid}.err").write_bytes(b"")
        out = B._run({"pid": victim.pid, "kill": True})
        assert "PID の使い回し" in out, out
        time.sleep(0.4)
        assert victim.poll() is None, "台帳の番号だけで別人を撃った"

        # ② 本人（起動時刻が合う）なら従来どおり止まる＝壁にしない。
        (d / f"{victim.pid}.meta.json").write_text(json.dumps(
            {"pid": victim.pid, "pgid": victim.pid, "cmd": "sleep 30",
             "cwd": None, "workspace": str(tmp_path / "ws"),
             "start_ticks": procutil.proc_start_ticks(victim.pid)}), encoding="utf-8")
        out = B._run({"pid": victim.pid, "kill": True})
        assert "PID の使い回し" not in out, out
        assert out.startswith("OK:"), out
        assert victim.poll() is not None or _wait_gone(victim), out
    finally:
        if victim.poll() is None:
            victim.kill()
        victim.wait(timeout=5)


def _wait_gone(proc, secs: float = 3.0) -> bool:
    import time
    deadline = time.time() + secs
    while time.time() < deadline:
        if proc.poll() is not None:
            return True
        time.sleep(0.05)
    return False
