"""Read が返す量を**窓に対して相対**に決めること。

★なぜ: 従来の上限 50,000 文字は、窓 16,384 トークンの推論器では**窓そのものより
  大きい**。1回の Read で会話が入らなくなり `context_exhausted` で走行が止まる。
  実測で 95 走行中 13 本が文脈切れで止まっており、その一因がこれ
  （もう一因は生成余白の確保で、別途修正済み）。
  `agent.py` の該当コメントも「本当の直しは道具の側」と書いていた。

★切ったことは必ず出力に書き、**続きの読み方を名指しする**。
  黙って切ると「全部読めた」と思い込む——この木で既に踏んでいる形。
"""
from __future__ import annotations

import laps.tools.read as read_mod


def test_窓が分からなければ従来どおり(monkeypatch) -> None:
    """★分からないときに勝手に狭めない（振る舞いを変えない）。"""
    monkeypatch.delenv("LAPS_N_CTX", raising=False)
    assert read_mod._text_cap() == read_mod._MAX_TEXT_CHARS


def test_狭い窓では上限も下がる(monkeypatch) -> None:
    monkeypatch.setenv("LAPS_N_CTX", "16384")
    cap = read_mod._text_cap()
    assert cap < read_mod._MAX_TEXT_CHARS
    # ★期待値は既定の割合から導く（literal だと既定を変えたとき落ちる）
    from laps.tools.limits import DEFAULT_OUT_CAP_FRACTION as _F
    assert cap == max(4000, int(16384 * _F * 2)), cap


def test_広い窓では従来の上限を超えない(monkeypatch) -> None:
    """★広ければいくらでも返してよい、ではない。1回で会話を押し出さない。"""
    monkeypatch.setenv("LAPS_N_CTX", "262144")
    assert read_mod._text_cap() == read_mod._MAX_TEXT_CHARS


def test_極端に狭くても最低限は返す(monkeypatch) -> None:
    """0 文字にすると Read が何の役にも立たなくなる。"""
    monkeypatch.setenv("LAPS_N_CTX", "2048")
    assert read_mod._text_cap() == 4000


def test_壊れた値は無視する(monkeypatch) -> None:
    for v in ("", "abc", "-1", "0"):
        monkeypatch.setenv("LAPS_N_CTX", v)
        assert read_mod._text_cap() == read_mod._MAX_TEXT_CHARS, v


def test_実際に切れて続きの読み方が出る(tmp_path, monkeypatch) -> None:
    """★通しで確認。上限を当てるだけでなく、次の呼び方が出ること。"""
    monkeypatch.setenv("LAPS_N_CTX", "16384")
    monkeypatch.setenv("LAPS_POLICY", "off")
    f = tmp_path / "big.txt"
    f.write_text("\n".join("x" * 100 for _ in range(500)))    # 約 50,000 字
    out = read_mod._run({"file_path": str(f)})
    assert "打ち切りました" in out, "上限に当たっていない＝この試験の前提が崩れている"
    assert "offset=" in out, "続きの読み方が示されていない"
    assert len(out) < 20000, f"窓に対して大きすぎる: {len(out)}"


def test_agentが窓を道具へ渡している() -> None:
    """★「書いたが繋いでいない」を防ぐ。道具は自分で窓を探れない。"""
    import inspect

    import laps.oc.base as agent_mod

    src = inspect.getsource(agent_mod.Agent._ctx_window)
    assert "LAPS_N_CTX" in src, "探った窓を道具へ渡していない"
    assert "setdefault" in src, "利用者が明示した値を上書きしている"


# ── 画像だけ上限が無かった（2026-09-02）─────────────────────────────────
#
# テキストには窓に対する予算（_text_cap）、PDF には _MAX_PDF_CHARS があるのに、
# 画像の data URI だけ**無制限**だった。1MB の PNG は base64 で 1.4MB＝
# 窓 16,384 トークンの推論器では 1 回の Read で会話が入らなくなる。
# しかもその base64 を読む相手が製品の中に一人もいない
# （`grep -rn "data:image" src/laps/` は 0 件・エンジンは text-only）。

def test_large_image_is_described_not_embedded(tmp_path):
    from laps.tools.read import _run

    big = tmp_path / "shot.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400_000)
    out = _run({"file_path": str(big)})
    assert "data:image" not in out, "巨大な画像がそのまま文脈へ流れ込んでいる"
    assert "bytes: 400,008" in out
    # ★2026-09-21: 文面を「base64 は載せていません（予算）」から「あなたには見えていません」へ。
    #   仕組みの説明ではモデルが「見た」と書くのを止められなかった（test_read_image_says_unseen）。
    assert "見えていません" in out          # 黙って落とさない
    assert "元データ" in out                # 次の一手がある（OS を問わない）


def test_small_image_still_embeds(tmp_path):
    from laps.tools.read import _run

    small = tmp_path / "icon.png"
    small.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    out = _run({"file_path": str(small)})
    assert "data:image/png;base64," in out


def test_embedding_can_be_re_enabled_for_multimodal(tmp_path, monkeypatch):
    from laps.tools.read import _run

    monkeypatch.setenv("LAPS_READ_IMAGE_MAX_B64", "10000000")
    big = tmp_path / "shot.png"
    big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 400_000)
    assert "data:image/png;base64," in _run({"file_path": str(big)})
