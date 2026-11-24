"""`ss` が無い環境でも待ち受けポートを答える（2026-09-15 の dogfood 発）。

★見つけ方: 出荷物 0.1.225 を素の `ubuntu:22.04` コンテナに入れ、**利用者として**
  「このホストで動いているサービスと、それぞれが使っているポートを教えてください」
  と頼んだ。返ってきたのは:

      ・現在の環境では直接ポート情報を取得できません
      ・systemctl や ss、netstat コマンドが利用できないため

  ★モデルは正直だった（捏造していない）。**欠けていたのは道具のほう**で、
  `listening` は `ss -ltnp` 決め打ちだった。最小のコンテナには iproute2 が入っていない。
  **`/proc/net/tcp` は Linux なら必ずある。**
"""
from __future__ import annotations

from laps.tools import system_state as S


def _proc_tcp(tmp_path, 行: list[str], name="tcp"):
    p = tmp_path / name
    p.write_text("  sl  local_address rem_address   st ...\n" + "\n".join(行) + "\n")
    return p


def test_LISTENだけを拾いポートは16進から戻す(tmp_path):
    # 0100007F:1F90 = 127.0.0.1:8080 / st=0A(LISTEN)、もう 1 本は 01(ESTABLISHED)
    f = _proc_tcp(tmp_path, [
        "   0: 0100007F:1F90 00000000:0000 0A 00000000:00000000 00:00000000 00000000",
        "   1: 0100007F:1F91 0100007F:ABCD 01 00000000:00000000 00:00000000 00000000",
    ])
    out = S._listening_from_proc(paths=((str(f), False),))
    assert "127.0.0.1:8080" in out
    assert "8081" not in out, "LISTEN 以外を拾っている"


def test_持ち主が分からないことを明記する(tmp_path):
    """★`ss -ltnp` は /proc/<pid>/fd を総なめして inode を突き合わせている。
    こちらはそれをしないので **PID/名前は出せない**。出せないものは出さないと書く。"""
    f = _proc_tcp(tmp_path, [
        "   0: 00000000:0016 00000000:0000 0A 00000000:00000000 00:00000000 00000000"])
    out = S._listening_from_proc(paths=((str(f), False),))
    assert "0.0.0.0:22" in out
    assert "PID" in out and "分かりません" in out


def test_読めなければNone(tmp_path):
    """/proc が無い環境（Windows 等）で例外にしない＝呼び出し側が次の手を選べる。"""
    assert S._listening_from_proc(paths=((str(tmp_path / "ない"), False),)) is None
