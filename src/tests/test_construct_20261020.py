"""★2026-10-20 Construct（手掛かりで思い出す記憶・oc/construct）。オーナー「導入してください。慎重に一つづつ正確に」「名前は Construct」。
設計 DESIGN-memory-20261020.md。ここは土台（手掛かり・置き場・点数・添える形）だけを確かめる。推論機は使わない。
"""
from __future__ import annotations

import time

import pytest

from laps.oc import construct as c


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("LAPS_CONSTRUCT", raising=False)
    return tmp_path


def terms(text):
    return [x.term for x in c.cues(text)]


def test_cues_pick_names_identifiers_and_japanese_words():
    t = terms("src/laps/oc/loop.py の _done_gate を直して")
    assert "loop.py" in t and "done_gate" in t and "src/laps/oc/loop.py" in t
    assert "dns" in terms("DNS の設定を書き換えた件")                  # 略語は強い手掛かり
    assert [x.strong for x in c.cues("DNS の件") if x.term == "dns"] == [True]
    assert [x.strong for x in c.cues("GPU の件") if x.term == "gpu"] == [False]   # どこにでも出る略語は弱い
    assert "ローファー" in terms("ローファーの下絵を TRELLIS で作って") and "trellis" in terms("TRELLIS で")
    # 一般の語・助詞・数字だけの語は手掛かりにしない
    assert not set(terms("please check the file and run tests 12345")) & {"please", "check", "file", "run", "tests", "12345"}
    assert "ファイル" not in terms("ファイルを確認してください")
    # 漢字の長い連なりは 3 文字ずつの弱い手掛かりも足す
    ws = {x.term: x.weight for x in c.cues("記憶検索強化を進める")}
    assert ws.get("記憶検索強化") == 0.7 and ws.get("検索強") == 0.4


def test_activation_follows_actr_shape():
    now = time.time()
    a = [round(c.activation([now - h * 3600], now), 2) for h in (1, 24, 168, 720, 8760)]
    assert a == [0.92, 0.71, 0.48, 0.31, 0.12]
    # 何度も使われた記憶は、1 回だけの記憶より強い（頻度）
    assert c.activation([now - 168 * 3600] * 5, now) > c.activation([now - 168 * 3600], now)
    assert c.activation([], now) == 0.0


def test_add_merges_the_same_gist_and_redacts(tmp_path):
    a = c.add("fact", "DNS を 8.8.8.8 に変えると名前が引けた", folder=tmp_path, source="tool", status="verified")
    b = c.add("fact", "DNS を  8.8.8.8 に変えると名前が引けた", folder=tmp_path, source="tool")   # 空白の違いは同じ物
    assert a == b
    con = c._connect()
    try:
        assert con.execute("SELECT count(*) FROM items").fetchone()[0] == 1
        assert len(c._uses(con.execute("SELECT uses FROM items").fetchone()[0])) == 2     # 使った時刻が増える
    finally:
        con.close()
    assert c.add("nonsense-kind", "x") is None and c.add("fact", "   ") is None
    # 秘密は伏せてから書く
    c.add("fact", "設定に password=hunter2-very-secret-value を入れた", folder=tmp_path)
    con = c._connect()
    try:
        texts = " ".join(r[0] for r in con.execute("SELECT gist||body||text FROM items"))
    finally:
        con.close()
    assert "hunter2-very-secret-value" not in texts


def test_search_prefers_this_folder_sources_and_strong_matches(tmp_path):
    here, other = tmp_path / "proj", tmp_path / "other"
    now = time.time()
    c.add("episode", "loop.py の _done_gate を直した（差し戻しの順番）", folder=here, ts=now - 86400)
    c.add("episode", "loop.py の _done_gate を直した（別のフォルダ）", folder=other, ts=now - 86400)
    c.add("fact", "loop.py は 4900 行ある", folder=here, source="model", status="unverified", ts=now - 86400)
    c.add("episode", "README を書いた", folder=here, ts=now - 3600)
    hits = c.search("loop.py の _done_gate をもう一度直して", folder=here, now=now)
    assert [h.gist for h in hits][:1] == ["loop.py の _done_gate を直した（差し戻しの順番）"]
    assert all("README" not in h.gist for h in hits)                   # 手掛かりの当たらない物は出ない
    order = [h.gist for h in hits]
    assert order.index("loop.py の _done_gate を直した（差し戻しの順番）") < order.index("loop.py の _done_gate を直した（別のフォルダ）")
    # モデルの推測で当たりも半分（loop.py だけ）＝しきい値に届かず添えない。しきい値を外して並べると下にいる。
    assert "loop.py は 4900 行ある" not in order
    s = {h.gist: h.score for h in c.search("loop.py の _done_gate をもう一度直して", folder=here, now=now,
                                           min_score=0.0, limit=10)}
    assert s["loop.py は 4900 行ある"] < s["loop.py の _done_gate を直した（差し戻しの順番）"]
    # 同じ当たりなら、確かめた事実はモデルの推測より上
    c.add("fact", "loop.py は 4900 行（wc で数えた）", folder=here, source="tool", status="verified", ts=now - 86400)
    s2 = {h.gist: h.score for h in c.search("loop.py の行数", folder=here, now=now, min_score=0.0, limit=10)}
    assert s2["loop.py は 4900 行（wc で数えた）"] > s2["loop.py は 4900 行ある"]
    # 他のフォルダを見ない指定
    assert all(h.folder != c.folder_key(other) for h in c.search("loop.py _done_gate", folder=here, other_folders=False, now=now))


def test_weak_single_match_is_not_returned(tmp_path):
    c.add("episode", "GPU の温度を見た", folder=tmp_path)
    assert c.search("GPU を使う", folder=tmp_path) == []                # 弱い手掛かり 1 つだけの当たりは返さない
    c.add("episode", "DNS の設定を戻した", folder=tmp_path)
    assert [h.gist for h in c.search("DNS はどうなった", folder=tmp_path)] == ["DNS の設定を戻した"]   # 強い手掛かりなら 1 つで当たる


def test_corrections_and_status_weights(tmp_path):
    now = time.time()
    c.add("correction", "WIN-VM は再起動しない（GPU が Code 43 になる）", folder="", source="user", ts=now - 30 * 86400)
    c.add("fact", "WIN-VM は再起動してよい", folder="", source="model", status="contradicted", ts=now - 3600)
    hits = c.search("WIN-VM を再起動して直す", folder=tmp_path, now=now)
    assert hits and hits[0].kind == "correction"                         # 古くても利用者の訂正が上
    assert all(h.status != "contradicted" for h in hits)                 # 外れた推測は点数が足りず出ない


def test_exclusions_and_limit(tmp_path):
    ids = [c.add("episode", f"trellis の形を作った その{i}", folder=tmp_path, session=f"s{i}") for i in range(5)]
    assert len(c.search("trellis で形を作る", folder=tmp_path)) == 3
    got = c.search("trellis で形を作る", folder=tmp_path, limit=10, exclude_sessions=("s0", "s1"), exclude_ids=(ids[2],))
    assert {h.gist for h in got} == {"trellis の形を作った その3", "trellis の形を作った その4"}


def test_fragments_label_provenance_but_hold_back_the_contents(tmp_path):
    """★2026-10-21 作り直し: 思い出すと断片だけ（何の・いつ・どこ・誰の記録か）。中身（確かめの命令・効いた手・場所）は開いた時だけ。"""
    c.set_current("", str(tmp_path))
    c.add("correction", "SNS に知らせてから推論機を止める", folder="", source="user")
    c.add("fact", "SNS の撮影は katcoder で揃える", folder=tmp_path, source="model", status="unverified",
          loc=".blackwall/FINDINGS.md")
    out = c.tool({"query": "SNS の撮影 推論機", "conversations": False})
    assert out.startswith("Construct: 2 fragments") and "These are only fragments" in out
    assert "user correction" in out and "said by the user" in out and "your own guess, unverified" in out
    assert "FINDINGS.md" not in out                                        # 場所は開いた時に出す
    assert c.fragment("episode", "依頼「add を直して」→ 完了・書き換え: app/calc.py・確かめ: make check-add") \
        == "依頼「add を直して」→ 完了"                                     # 書き換えたファイル・確かめの命令は出さない
    assert c.fragment("lesson", "失敗「No module named yaml」→ 効いた手「pip install pyyaml」") == "失敗「No module named yaml」"
    assert len(c.fragment("fact", "あ" * 300)) == c.FRAGMENT_CHARS


def test_prune_keeps_corrections_and_intentions(tmp_path, monkeypatch):
    monkeypatch.setattr(c, "MAX_ITEMS", 5)
    c.add("correction", "古い訂正", folder="", source="user", ts=1.0)
    for i in range(8):
        c.add("episode", f"出来事 {i}", folder=tmp_path, ts=10.0 + i)
    con = c._connect()
    try:
        kinds = [r[0] for r in con.execute("SELECT kind FROM items")]
        gists = [r[0] for r in con.execute("SELECT gist FROM items")]
    finally:
        con.close()
    assert len(kinds) == 5 and "correction" in kinds and "出来事 7" in gists and "出来事 0" not in gists


def test_switch_off(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_CONSTRUCT", "0")
    assert c.add("episode", "DNS を戻した", folder=tmp_path) is None
    assert c.search("DNS", folder=tmp_path) == []
    assert not c.db_path().exists()


# ── 取り込み（いまの置き場が正本・Construct は索引だけ）──────────────────────────

WORKLOG = """# 作業ログ

## 環境
- 作業フォルダ: x

## メモ
（Blackwall と利用者の覚え書き。道具の置き場・動かし方・決めたことなど、次の会話でも要ることを書く）

## 作業の記録
### 2026-10-20 12:00 ・ 作業中（途中の記録・止められた可能性もある） ・ 5 手番 ・ 3 分
- 依頼: 「途中の記録」
<!-- run:20261020_120000_aaaaaa-1792000000 -->

### 2026-10-19 09:00 ・ 完了 ・ 12 手番 ・ 4 分
- 依頼: 「release_chain.sh で 0.1.551 を出荷して」
- 書き換えたファイル: src/pyproject.toml
- 確認: ✓ 通過（`grep RELEASE_OK /tmp/rel551.log`）
<!-- run:20261019_090000_bbbbbb-1791900000 -->

### 2026-10-18 08:00 ・ 中断 ・ 3 手番 ・ 1 分
- 依頼: 「TRELLIS の環境を作って」
<!-- run:20261018_080000_cccccc-1791800000 -->

### 2026-10-17 07:00 ・ 完了 ・ 6 手番 ・ 1 分
- 依頼: 「モデルが自分で書き足した偽の記録（走行の印が無い）」
"""

FINDINGS = """# 分かったこと（Blackwall の帳面）

## 確かめた事実
- [2026-10-19 09:10 #20261019_090000_bbbbbb] release_chain.sh の成否は /tmp/relNNN.log の RELEASE_OK の行で見る（終了コードは 0 のまま）。
- [2026-10-19 09:10 #20261019_090000_bbbbbb] release_chain.sh:
- 短い行

## 行き止まり
- [2026-10-19 09:11] spconv-cu120 は Python 3.12 の配布物が無いので入らない（cu121 を使う）。
"""


def _ws(tmp_path):
    ws = tmp_path / "ws"
    (ws / ".blackwall").mkdir(parents=True)
    (ws / ".blackwall" / "WORKLOG.md").write_text(WORKLOG, encoding="utf-8")
    (ws / ".blackwall" / "FINDINGS.md").write_text(FINDINGS, encoding="utf-8")
    return ws


def _rows(sql="SELECT kind, gist, session, run, prev, loc, source, status, uses FROM items ORDER BY ts"):
    con = c._connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_ingest_worklog_findings_and_lessons(tmp_path):
    ws = _ws(tmp_path)
    lessons = c.db_path().parent / "lessons.jsonl"
    lessons.parent.mkdir(parents=True, exist_ok=True)
    lessons.write_text('{"sig": "No matching distribution found for spconv-cu120", "fix": "pip install spconv-cu121", '
                       '"count": 2, "ts": 1791000000, "queries": ["spconv cp312"]}\n', encoding="utf-8")
    got = c.ingest(str(ws))
    assert got == {"worklog": 2, "findings": 2, "lessons": 1}             # 途中の記録・印の無い記録・見出しだけの行・短い行は足さない
    rows = _rows()
    eps = [r for r in rows if r[0] == "episode"]
    assert [e[1] for e in eps] == ["依頼「TRELLIS の環境を作って」→ 中断",
                                   "依頼「release_chain.sh で 0.1.551 を出荷して」→ 完了・書き換え: src/pyproject.toml"
                                   "・確かめ: grep RELEASE_OK /tmp/rel551.log"]       # 通った確かめの命令も要旨に
    assert eps[1][2] == "20261019_090000_bbbbbb" and eps[1][3] == "20261019_090000_bbbbbb-1791900000"
    assert eps[1][7] == "verified" and eps[0][7] == ""                    # ✓ 通過の記録は確かめ済み
    con = c._connect()
    try:
        first_id = con.execute("SELECT id FROM items WHERE gist LIKE '依頼「TRELLIS%'").fetchone()[0]
    finally:
        con.close()
    assert eps[1][4] == first_id                                          # 時間の隣（前の出来事）を繋ぐ
    assert eps[1][5].endswith("WORKLOG.md (2026-10-19 09:00)")
    facts = [r for r in rows if r[0] in ("fact", "dead_end")]
    assert {(f[0], f[6], f[7]) for f in facts} == {("fact", "model", "verified"), ("dead_end", "model", "")}
    assert [r for r in rows if r[0] == "lesson"][0][1] == ("失敗「No matching distribution found for spconv-cu120」"
                                                           "→ 効いた手「pip install spconv-cu121」")
    # 変わっていなければ何も読まない・使った回数も増えない
    assert c.ingest(str(ws)) == {"worklog": 0, "findings": 0, "lessons": 0}
    assert all(len(c._uses(r[8])) == 1 for r in _rows())
    # 作業ログに 1 件増えたら、その 1 件だけ足す
    p = ws / ".blackwall" / "WORKLOG.md"
    p.write_text(p.read_text(encoding="utf-8").replace(
        "## 作業の記録\n", "## 作業の記録\n### 2026-10-20 13:00 ・ 完了 ・ 2 手番 ・ 20 秒\n- 依頼: 「MCP を入れ直して」\n"
        "<!-- run:20261020_130000_dddddd-1792000100 -->\n\n"),
        encoding="utf-8")
    assert c.ingest(str(ws))["worklog"] == 1


def test_ingested_records_are_found_by_cues(tmp_path):
    ws = _ws(tmp_path)
    c.ingest(str(ws))
    hits = c.search("0.1.551 を release_chain.sh で出荷する", folder=ws)
    assert hits and hits[0].kind in ("episode", "fact") and "0.1.551" in hits[0].matched
    dead = c.search("spconv-cu120 が入らない", folder=ws)
    assert dead and dead[0].kind == "dead_end"
    assert c.search("全く関係のない話題", folder=ws) == []




# ── 3/7 走行の始め: 添えない（2026-10-21 作り直し）。呼ぶべき場面かを数えるだけ ──────────────────

def _worklog(entries):
    body = "\n\n".join(f"### 2026-10-{d:02d} 09:00 ・ 完了 ・ 4 手番 ・ 1 分\n- 依頼: 「{req}」\n<!-- run:s{d}-{d} -->"
                         for d, req in entries)
    return ("# 作業ログ\n\n## 環境\n- x\n\n## メモ\n（Blackwall と利用者の覚え書き。道具の置き場・動かし方・決めたことなど、"
            "次の会話でも要ることを書く）\n\n## 作業の記録\n" + body + "\n")


def _agent(tmp_path, monkeypatch, entries, script):
    from tests.test_done_after_check_20261018 import FakeBackend
    from laps.oc import loop
    monkeypatch.setenv("LAPS_MODEL_FAMILY", "qwen")
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ws = tmp_path / "ws"
    (ws / ".blackwall").mkdir(parents=True, exist_ok=True)
    (ws / ".blackwall" / "WORKLOG.md").write_text(_worklog(entries), encoding="utf-8")
    monkeypatch.chdir(ws)
    a = loop.OcAgent()
    a._n_ctx = 131072
    a.backend = FakeBackend(script)
    return a


def _first_request(a):
    return [m for m in a.backend.sent[0] if m.get("role") == "user"][-1]["content"]


ENTRIES = [(19, "README の誤字を直して"), (18, "ログの色を変えて"), (17, "Android の文言を直して"),
           (12, "spconv-cu121 で TRELLIS の環境を作った"), (10, "MCP サーバを 0.1.551 に入れ直した")]










# ── 4/7 道具 Construct（記憶と過去の会話を自分で引く）────────────────────────────

def _conv(home, sid, msgs):
    import json as _j
    d = home / "sessions" / sid
    d.mkdir(parents=True, exist_ok=True)
    (d / "messages.jsonl").write_text("".join(_j.dumps(m, ensure_ascii=False) + "\n" for m in msgs), encoding="utf-8")
    return d


def test_tool_finds_memories_and_past_conversations(tmp_path):
    home = c.db_path().parent
    ws = _ws(tmp_path)
    c.ingest(str(ws))
    c.set_current("20261021_100000_cur000", str(ws))
    _conv(home, "20261015_090000_old111", [
        {"role": "user", "content": "[アプリからの指示]\nWorking directory: x\n\n[利用者]\nspconv-cu120 が入らないので直して"},
        {"role": "assistant", "content": "spconv-cu121 に替えたら Python 3.12 でも入りました。"}])
    _conv(home, "20261021_100000_cur000", [{"role": "user", "content": "spconv-cu120 をいま調べている"}])
    out = c.tool({"query": "spconv-cu120"})
    assert out.startswith("Construct: ")
    assert "dead end" in out and "spconv-cu120 は Python 3.12" in out
    assert "Past conversations: 1" in out and "2026-10-15 09:00 · user" in out
    assert "[利用者]" not in out and "アプリからの指示" not in out              # 包みは外す
    assert "20261021_100000_cur000" not in out                            # いまの会話は引かない
    # 添えた回数を数える
    con = c._connect()
    try:
        assert con.execute("SELECT hits FROM items WHERE kind='dead_end'").fetchone()[0] >= 1
    finally:
        con.close()


def test_tool_abstains_and_validates(tmp_path):
    c.set_current("", str(tmp_path))
    assert c.tool({}).startswith("Error: give query")
    assert c.tool({"query": "の を に"}).startswith("No usable words")
    out = c.tool({"query": "zzzunknownthing.py の件"})
    assert out.startswith("No memory matches") and "do not guess" in out
    assert "recent conversations" not in c.tool({"query": "zzzunknownthing.py", "conversations": False})


def test_tool_scope_folder(tmp_path):
    here, other = tmp_path / "a", tmp_path / "b"
    c.add("episode", "trellis_gen.py で形を作った", folder=other)
    c.set_current("", str(here))
    assert "another folder (b)" in c.tool({"query": "trellis_gen.py", "conversations": False})
    assert c.tool({"query": "trellis_gen.py", "scope": "folder", "conversations": False}).startswith("No memory")


def test_open_needs_a_reason_then_shows_the_contents_and_counts(tmp_path):
    """開くのはモデルの判断: 今の作業との関係（why）を書かせてから中身を返す。開いた回数と使った時刻を足す。"""
    ws = _ws(tmp_path)
    c.ingest(str(ws))
    c.set_current("now", str(ws))
    out = c.tool({"query": "release_chain.sh 0.1.551", "conversations": False})
    first = out.split("\n")[1]
    assert first.startswith("#") and "確かめ" not in first and "grep RELEASE_OK" not in first   # 断片に中身は出さない
    iid = int(first[1:].split(" ")[0])
    assert c.tool({"open": iid}).startswith("Error: say in why")                       # 理由なしでは開かない
    assert c.tool({"open": iid, "why": "関係ある"}).startswith("Error: say in why")       # 短すぎる理由も
    con = c._connect()
    try:
        assert con.execute("SELECT used FROM items WHERE id=?", (iid,)).fetchone()[0] == 0
    finally:
        con.close()
    got = c.tool({"open": f"#{iid}", "why": "今の出荷の確かめ方を、前に通った命令から知りたい"})
    assert got.startswith(f"Memory #{iid} [") and "grep RELEASE_OK /tmp/rel551.log" in got
    assert "Details: .blackwall/WORKLOG.md (2026-10-19 09:00)" in got and "record of that time, not of now" in got
    assert "before #" in got                                               # 時間の隣（前の出来事）をたどれる
    con = c._connect()
    try:
        used, uses = con.execute("SELECT used, uses FROM items WHERE id=?", (iid,)).fetchone()
    finally:
        con.close()
    assert used == 1 and len(c._uses(uses)) == 2
    assert c.tool({"open": 99999, "why": "今の作業と同じ失敗だったので"}).startswith("Error: there is no memory #99999")
    assert c.tool({"open": "x", "why": "今の作業と同じ失敗だったので"}).startswith("Error: open takes the id")


def test_fragments_carry_links_to_recall_with_next(tmp_path):
    c.set_current("", str(tmp_path))
    c.add("dead_end", "spconv-cu120 は入らない", folder=tmp_path, source="model",
          body="Python 3.12 では spconv-cu121 と cumm-cu121 を使う。CUDA_HOME は cudaroot の shim")
    out = c.tool({"query": "spconv-cu120", "conversations": False})
    line = out.split("\n")[1]
    assert "links: " in line and "spconv-cu121" in line and "spconv-cu120" not in line.split("links: ")[1]


def test_memory_note_is_in_every_prompt_form_when_the_tool_is_given(monkeypatch):
    from laps.oc import prompt
    names = ["Bash", "Read", "Construct", "Done"]
    for kw in ({}, {"slim": True}, {"slim": True, "local": True}):
        sp = prompt.system_prompt("m", "/tmp", tool_names=names, **kw)
        assert "# Your memory" in sp and "Memories do not come to you by themselves" in sp, kw
        assert "# Your memory" not in prompt.system_prompt("m", "/tmp", tool_names=["Bash", "Read", "Done"], **kw), kw
    monkeypatch.setenv("LAPS_CONSTRUCT", "0")
    assert "# Your memory" not in prompt.system_prompt("m", "/tmp", tool_names=names)


def test_refers_to_past():
    for t in ("前と同じやり方で直して", "前回の続き", "この前の件", "do it like last time"):
        assert c.refers_to_past(t), t
    for t in ("README を直して", "名前を引けない", "before you start, read it"):
        assert not c.refers_to_past(t), t




# ── 5/7 利用者の訂正を覚える ─────────────────────────────────────────────────────

class _CallingBackend:
    """台本の要素が関数なら呼んで、その返り値を返す（割り込みを手番の途中で入れるため）。"""
    def __init__(self, script):
        from tests.test_done_after_check_20261018 import FakeBackend
        self._fb = FakeBackend([])
        self.script = list(script)
        self.usage, self.last_prompt_tokens, self.cfg, self.sent = self._fb.usage, 1000, self._fb.cfg, []

    def soft_reset(self):
        pass

    def close(self):
        pass

    def chat_with_tools(self, send, tools, **kw):
        self.sent.append(send)
        from tests.test_done_after_check_20261018 import DONE
        item = self.script.pop(0) if self.script else ("(no more)", [DONE])
        return item() if callable(item) else item

    def chat(self, send, **kw):
        return "## Objective\n- summary"


def _agent2(tmp_path, monkeypatch, script):
    from laps.oc import loop
    monkeypatch.setenv("LAPS_MODEL_FAMILY", "qwen")
    monkeypatch.setenv("LAPS_SESSIONS", str(tmp_path / "sess"))
    monkeypatch.setenv("LAPS_POLICY", "trusted")
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    monkeypatch.chdir(ws)
    a = loop.OcAgent()
    a._n_ctx = 131072
    a.backend = _CallingBackend(script)
    return a, ws


def _corrections():
    con = c._connect()
    try:
        return con.execute("SELECT gist, body, source, folder, uses FROM items WHERE kind='correction'").fetchall()
    finally:
        con.close()


def test_correction_detector():
    for t in ("違う、そうじゃなくて Linux の方", "WIN-VM は再起動しないで", "勝手に DNS を書き換えないで",
              "DNS ではなく hosts を直して", "元に戻して", "don't touch the config",
              "calc.py には型ヒントを付けないでください", "make は使わないで", "本番には触らないように"):
        assert c.is_correction(t), t
    for t in ("README も見ておいて", "ありがとう、続けて", "stopwatch.py を読んで", "はい", "わからないので調べて",
              "動かないでいる原因を調べて"):
        assert not c.is_correction(t), t








def test_shadow_start_skips_what_is_already_in_context(tmp_path):
    ws = tmp_path / "ws"
    iid = c.add("correction", "WIN-VM は再起動しないで（Code 43 になる）", folder=ws, source="user", session="old")
    assert c.shadow_start("WIN-VM を再起動して", str(ws), session="now") == [iid]
    seen = ["<worklog>- 依頼: 「WIN-VM は再起動しないで（Code 43 になる）」</worklog>"]
    assert c.shadow_start("WIN-VM を再起動して", str(ws), context=seen, session="now") == []
    assert c.shadow_start("WIN-VM を再起動して", str(ws), session="old") == []    # その会話の記憶は数えない


# ── 6/7 会話をまたぐやり残し ────────────────────────────────────────────────────

def _todo(*items):
    return {"name": "TodoWrite", "input": {"todos": [{"content": t, "status": st} for t, st in items]}}






# ── 7/7 動いている途中（失敗の時・初めて触るファイルは数えるだけ・前に訂正された操作は止める）──────────











# ── 段 2: 思い出した結果で書き直す（再固定化）・忘れる ──────────────────────────────

def _item(iid):
    con = c._connect()
    try:
        return con.execute("SELECT hits, used, good, bad, status, uses FROM items WHERE id=?", (iid,)).fetchone()
    finally:
        con.close()


def test_novel_terms_exclude_the_cues_that_recalled_it():
    t = c.novel_terms("spconv-cu120 spconv-cu121 python 配布物", "spconv-cu120 は入らない（spconv-cu121 を使う）",
                      {"spconv-cu120"})
    assert "spconv-cu121" in t and "spconv-cu120" not in t
    assert "配布物" not in t                                                # 短い弱い語は偶然の一致が多いので使わない


def test_feedback_good_bad_contradicted_and_restored(tmp_path):
    iid = c.add("dead_end", "spconv-cu120 は入らない（spconv-cu121 を使う）", folder=tmp_path, source="model")
    trig = {"spconv-cu120"}
    # 開いたが当てはめなかった（新しい語が出ない）＝何も書き換えない
    assert c.feedback([(iid, trig, "pip install torch", "")], True)["applied"] == 0
    # 当てはめて、うまくいった＝効いた
    out = c.feedback([(iid, trig, "pip install spconv-cu121", "")], True)
    assert out == {"applied": 1, "good": 1, "bad": 0, "contradicted": 0, "restored": 0}
    # 使われて、その命令が失敗した＝外れた。2 回外れて効いたより多ければ「外れた」の印
    c.feedback([(iid, trig, "pip install spconv-cu121", "pip install spconv-cu121")], False)
    out = c.feedback([(iid, trig, "pip install spconv-cu121", "pip install spconv-cu121")], False)
    assert out["contradicted"] == 1
    hits, used, good, bad, status, uses = _item(iid)
    assert (used, good, bad, status) == (0, 1, 2, "contradicted") and len(c._uses(uses)) == 1   # 開いた回数は open で数える
    # また効けば印を外す（効いたが上回ったら）
    c.feedback([(iid, trig, "spconv-cu121", "")], True)
    out = c.feedback([(iid, trig, "spconv-cu121", "")], True)
    assert out["restored"] == 1 and _item(iid)[4] == ""
    # 利用者の訂正は、効いた・外れたで書き換えない
    cid = c.add("correction", "resolved.conf を書き換えないで", folder=tmp_path, source="user")
    c.feedback([(cid, set(), "cat resolved.conf", "cat resolved.conf")], False)
    assert _item(cid)[1:5] == (0, 0, 0, "")


def test_utility_weights_and_value_based_prune(tmp_path, monkeypatch):
    assert c.utility(0, 0, 0, 0) == 1.0
    assert c.utility(3, 3, 3, 0) > 1.0 > c.utility(3, 3, 0, 3)
    assert c.utility(5, 0, 0, 0) == 0.7                                    # 何度見せても開かれない＝雑音として下げる
    now = time.time()
    good = c.add("fact", "trellis_gen.py の形は y が上", folder=tmp_path, ts=now - 86400)
    bad = c.add("fact", "trellis_gen.py の形は z が上", folder=tmp_path, ts=now - 86400)
    con = c._connect()
    try:
        con.execute("UPDATE items SET used=3, good=3 WHERE id=?", (good,))
        con.execute("UPDATE items SET used=3, bad=3 WHERE id=?", (bad,))
        con.commit()
    finally:
        con.close()
    s = {h.id: h.score for h in c.search("trellis_gen.py の形の向き", folder=tmp_path, min_score=0, limit=5, now=now)}
    assert s[good] > s[bad]
    # 上限を超えたら、一度も使われていない古い物から外す（使われた物は古くても残る）
    monkeypatch.setattr(c, "MAX_ITEMS", 3)
    for i in range(3):
        c.add("episode", f"新しい出来事 {i}", folder=tmp_path, ts=now + i)
    con = c._connect()
    try:
        ids = {r[0] for r in con.execute("SELECT id FROM items")}
    finally:
        con.close()
    assert good in ids and bad in ids and len(ids) == 3


def _recall_open(iid, why="the same spconv-cu120 error as now; it may say what to install instead"):
    return [("覚えがあるか引きます。", [{"name": "Construct", "input": {"query": "spconv-cu120"}}]),
            ("#%d が今の失敗と同じです。" % iid, [{"name": "Construct", "input": {"open": iid, "why": why}}])]






# ── 段 3a: 夜の整理（プロジェクトの要点）──────────────────────────────────────────

class _CardBackend:
    def __init__(self, text):
        self.text, self.sent = text, []

    def chat(self, messages, **kw):
        self.sent.append((messages, kw))
        return self.text


def _seed(ws):
    now = time.time()
    ids = {
        "corr": c.add("correction", "release_chain.sh は版を 5 か所そろえてから流す", folder=ws, source="user", ts=now - 50),
        "dead": c.add("dead_end", "spconv-cu120 は Python 3.12 で入らない（spconv-cu121 を使う）", folder=ws, source="model", ts=now - 40),
        "fact": c.add("fact", "成否は /tmp/relNNN.log の RELEASE_OK の行で見る", folder=ws, source="model", status="verified", ts=now - 30),
        "ep1": c.add("episode", "依頼「0.1.564 を出荷して」→ 完了", folder=ws, ts=now - 20),
        "ep2": c.add("episode", "依頼「MCP を入れ直して」→ 完了", folder=ws, ts=now - 10),
        "wrong": c.add("fact", "release は終了コードで判定できる", folder=ws, source="model", status="contradicted", ts=now - 5),
        "old_schema": c.add("schema", "古い要点", folder=ws, source="model", ts=now - 60),
    }
    return ids


def test_replay_order_and_exclusions(tmp_path):
    ids = _seed(tmp_path)
    notes = c.replay_notes(str(tmp_path))
    kinds = [k for _, k, _ in notes]
    assert kinds[0] == "correction" and kinds.index("dead_end") < kinds.index("fact") < kinds.index("episode")
    got = {i for i, _, _ in notes}
    assert ids["wrong"] not in got and ids["old_schema"] not in got        # 外れた印・前の要点は入れない（自己汚染）
    assert "(correction, said by the user)" in notes[0][2]


def test_consolidate_keeps_only_cited_bullets_and_reindexes(tmp_path):
    ids = _seed(tmp_path)
    card = (f"## How to build and run\n- 版を 5 か所そろえてから release_chain.sh を流す [n{ids['corr']}]\n"
            f"- 作り話の手順 [n99999]\n- 引用の無い行\n## How to check\n- RELEASE_OK の行で成否を見る [n{ids['fact']}, n{ids['ep1']}]\n"
            f"## Rules and the user's preferences\n- (none)\n## Pitfalls\n- spconv-cu120 は入らない [n{ids['dead']}]\n## Main files\n- (none)\n")
    be = _CardBackend(card)
    res = c.consolidate(str(tmp_path), be)
    assert res["bullets"] == 3 and res["dropped"] == 2
    assert be.sent[0][1]["chat_template_kwargs"] == {"enable_thinking": False}
    text = (tmp_path / ".laps" / "PROJECT.md").read_text(encoding="utf-8")
    assert text.startswith("# プロジェクトの要点（Construct が") and "未確認" in text
    assert "作り話" not in text and "引用の無い行" not in text and "## Main files\n- (none)" in text
    con = c._connect()
    try:
        schema = [r[0] for r in con.execute("SELECT gist FROM items WHERE kind='schema' ORDER BY id")]
    finally:
        con.close()
    assert "古い要点" not in schema and len(schema) == 3                     # 前の要点の箇条は入れ直す
    assert all("[n" not in g for g in schema)                              # 索引の要旨には番号を残さない
    hit = c.search("RELEASE_OK の行で成否", folder=tmp_path, kinds=("schema",))
    assert hit and hit[0].loc.endswith("PROJECT.md (How to check)")


def test_consolidate_skips_with_few_notes_or_no_usable_bullets(tmp_path):
    c.add("fact", "一つだけの記録です、とても短い", folder=tmp_path)
    assert c.consolidate(str(tmp_path), _CardBackend("x"))["skipped"] == "too few notes"
    _seed(tmp_path / "w2")
    res = c.consolidate(str(tmp_path / "w2"), _CardBackend("## How to check\n- 引用なし\n"))
    assert res["skipped"] == "no usable bullets" and not (tmp_path / "w2" / ".laps" / "PROJECT.md").exists()




# ── 段 3b: 夜に自動で整理する（serve が空いている時だけ）──────────────────────────────

def _local_at(hour):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0, 0, 0, -1))


class _Cfg:
    def __init__(self, url, model="blackwall-coder"):
        self.base_url, self.model = url, model


def test_night_is_off_by_default(monkeypatch):
    assert not c.nightly_enabled() and not c.nightly_due(_local_at(2))     # 既定は切る（オーナー 10-21）
    assert c.nightly(lambda: False, lambda: None, now=_local_at(2)) == {"folders": 0, "bullets": 0, "skipped": 0,
                                                                       "errors": 0, "stopped": 0}


def test_night_window_and_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("LAPS_CONSTRUCT_NIGHT", "1")
    assert c.nightly_due(_local_at(2)) and not c.nightly_due(_local_at(14)) and not c.nightly_due(_local_at(5))
    c.nightly(lambda: False, lambda: None, now=_local_at(2))                # 今夜やった（記録が無くても印は付く）
    assert not c.nightly_due(_local_at(2) + 3600)                          # 同じ夜にもう一度はしない
    assert c.nightly_due(_local_at(2) + 86400)                             # 次の夜はやる
    monkeypatch.setenv("LAPS_CONSTRUCT_NIGHT", "0")
    assert not c.nightly_due(_local_at(2) + 86400)


def test_only_the_local_inference_server_is_used():
    be = _CardBackend("")
    be.cfg = _Cfg("http://192.0.2.160:8741/v1")
    assert c.local_backend_ok(be)
    for url, model in (("http://192.0.2.9:8745/v1", "claude-haiku-5-5"), ("http://192.0.2.9:8745/v1", "x"),
                       ("https://api.anthropic.com/v1", "x"), ("http://192.0.2.160:8741/v1", "claude-opus-5-5")):
        be.cfg = _Cfg(url, model)
        assert not c.local_backend_ok(be), url




