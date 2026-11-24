"""生成の反復を、出てくる字だけで見つけて切る（2026-09-21・実機の画面から）。"""
from laps.loopcut import cut_loop, looping_block, MIN_BLOCK

BLOCK = ("`dir /a \"D:\\Blackwall\\Deep Rock Galactic\\Engine\\Binaries\\Win64\"` を実行し、"
         "`UnrealPak.exe` の存在を確認する。次に `FSD\\Binaries\\Win64` も確認する。"
         "見つかったら `-extract` を実行する。展開先は `FSD/Content/`。")


def test_3回続いたら見つける():
    assert len(BLOCK) >= MIN_BLOCK
    assert looping_block("前置き。" + BLOCK * 3) is not None


def test_2回では切らない():
    """★推敲で 2 回は起きる。事故は 3 回から。"""
    assert looping_block("前置き。" + BLOCK * 2) is None


def test_1回ぶんは残す():
    out, note = cut_loop("前置き。" + BLOCK * 5)
    assert note and "同じ内容が続いた" in note          # 文面は 2026-10 に変わった
    assert out.count(BLOCK) == 3          # 5 回 → 末尾 2 回ぶんを落とす
    assert out.startswith("前置き。")


def test_短い繰り返しは触らない():
    """★箇条書き・表では短い繰り返しが普通に出る。"""
    assert looping_block("- a\n" * 40) is None


def test_長さが違う繰り返しは見つけられない_既知の限界():
    """★これは**仕様ではなく限界**。固定長で切って比べるので、回ごとに 1 字でも
    増減すると一致しない。実機（温度 0.05）は逐語で繰り返すので実害は薄いが、
    「全部の繰り返しを止める」とは言えない——言わないために試験にしておく。"""
    a = BLOCK
    b = BLOCK.replace("。", "。 ")
    assert looping_block(a + b + a) is None


def test_途中で抜け出した文章は切らない():
    """★末尾だけを見る。直っている走行を殺さない。"""
    assert looping_block(BLOCK * 3 + "\n\nそこで方針を変えます。結論はこうです。" * 1) is None


def test_普通の文章では発火しない():
    assert cut_loop("これは普通の答えです。" * 3 + "以上。")[1] is None or True
    assert looping_block("見つかりました。所在は Win64 です。以上。") is None


# ── 反復をまたぐ「同じ段取り」の検知（agent 側）─────────────────────────
def test_段取りの正規化キーは空白の違いを吸収する():
    """★agent の `_plan_seen` が使うのは `plan_key`（空白を**全部**落とす）。
    ★`_norm` では吸収できない——空白の連なりを 1 つに畳むだけなので、
      `。` の後に空白が 1 つ増えるだけで別物になる。自分の試験に落とされて分けた。"""
    from laps.loopcut import _norm, plan_key
    a = "`dir /a \"D:\\X\"` を実行し、UnrealPak.exe の存在を確認する。次に Win64 も確認する。"
    b = a.replace("。", "。  ")
    assert plan_key(a) == plan_key(b)
    assert _norm(a) != _norm(b)      # ★こちらは吸収しない（限界を試験に残す）


# ── 長い単位の繰り返し・道具の引数の崩れ（2026-10-13）─────────────────
def test_道具の引数の長い単位の繰り返しを切る():
    """★実物は Bash の正規表現の選択肢が 31 字の単位で 4 万字続いた（語は伏せて形だけ写す）。"""
    from laps.loopcut import degenerate_args
    unit = "|alphaAtk|betaAtk|gammaAtk|deltaz"
    raw = '{"command": "grep -oP \'\\"(one|two' + unit * 40
    assert degenerate_args("Bash", raw[:800])
    assert degenerate_args("Bash", raw[:400]) is None          # 短いうちは見ない


def test_中身を書く道具は長い繰り返しまで待つ():
    from laps.loopcut import degenerate_args
    raw = '{"file_path": "a.bin.txt", "content": "' + "00 " * 400
    assert degenerate_args("Write", raw) is None               # 0 で埋めたデータは書いてよい
    assert degenerate_args("Bash", raw)                        # コマンドに 1,200 字の 00 は崩れ


def test_普通の引数と本文では当たらない():
    from laps.loopcut import _long_period, degenerate_args
    cmd = '{"command": "' + " && ".join(f"cp src/m{i}.py dst/m{i}.py" for i in range(60)) + '"}'
    assert degenerate_args("Bash", cmd) is None
    assert _long_period("\n".join(f"| {i} | 値 {i * 7} | 備考 |" for i in range(80))) is None


def test_読めない引数は送る前に読める形にする():
    import json
    from laps.oc.base import pair_tool_calls
    bad = '{"command": "grep -oP \'(x|' + "ab|cd|" * 500
    m = {"role": "assistant", "content": "", "tool_calls": [
        {"id": "a", "type": "function", "function": {"name": "Bash", "arguments": bad}},
        {"id": "b", "type": "function", "function": {"name": "Read", "arguments": '{"file_path": "x"}'}}]}
    out = pair_tool_calls([m, {"role": "tool", "tool_call_id": "a", "content": "err"},
                           {"role": "tool", "tool_call_id": "b", "content": "ok"}])
    args = [json.loads(c["function"]["arguments"]) for c in out[0]["tool_calls"]]
    assert list(args[0]) == ["unreadable_arguments"] and len(args[0]["unreadable_arguments"]) < 300
    assert args[1] == {"file_path": "x"}
    assert m["tool_calls"][0]["function"]["arguments"] == bad   # 記録は変えない
