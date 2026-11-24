# ★2026-10-30 既定の中核は blackwall/engine になった。旧い中核の試験（test_serve* など）はこの印で旧い中核を起こす。
#   新しい中核の試験（test_engine_20261030.py）は子プロセスの環境からこの印を外す。
import os as _os_engine
_os_engine.environ.setdefault("LAPS_ENGINE", "legacy")

"""Tool tests write real files, so they need a mode that permits writing.

Kept as `workspace` (not `off`) on purpose: the suite then also exercises the
policy gate on every write, so a regression that breaks legitimate writes shows
up here instead of in production. Temp dirs live under /tmp, which policy treats
as the write root for the duration of the tests.

★2026-09-05（第4回c）: 「/tmp が試験中の workspace root」は**もう所有権の話では
  ない**。`procutil.canonical_workspace()` には床が入っていて、`/`・`$HOME`・
  深さ 1 の場所は作業場として受けない——`/tmp` は深さ 1 なので `NO_WORKSPACE`
  に落ちる。つまり試験の既定では **kill 系のゲートから見て「自分の物」は何も
  無い**（撃たない側へ倒れる。書き込みの許可とは別の軸）。
  プロセスの所有権や台帳の通行証を測る試験は、`LAPS_WORKSPACE` を
  `tmp_path/ws`（深さ 3 以上）に自分で立てること。
  ⚠ 深さ 2 は床を通る（`/srv/shared` のような実在の場所も「作業場」になり得る）。
    床が守るのは「`/` や `$HOME` を渡されてホスト全部が中になる」形だけで、
    浅いが正当な作業場を弾く物差しではない。
"""
import ipaddress
import os
import socket
import subprocess

import pytest


# ★試験は LAN（推論機・中継・記録の受け口）へ出ない（2026-10-15）。
#   lab.yml の test でラボから全体の試験を回したところ、test_audit_ws_guard_notice_wiring などが本物の Backend を作り、
#   既定の接続先（推論機:8741）へ要求を出し続けた＝オーナーと共用の推論機（P40）を 30 分以上 100% で使った。
#   llama.cpp は共有の KV が溢れると処理中の全部の枠を止めるので、オーナーの走行まで巻き込みうる。
#   ① 既定の接続先を、すぐに断られる住所へ向ける（import の時点で＝モジュールの読み込みで値を読む物にも効く・
#      子プロセスにも受け継がれる）。試験が自分で setenv すればそちらが勝つ（下の autouse でも同じ値を入れ直す）。
#   ② それでも LAN（192.168.0.0/16）へつなごうとした物は、この試験のプロセスの中では接続の失敗にする＝
#      本物へ行く試験がその場で見つかる。子プロセスは ① だけが守る。
_UNREACHABLE = "127.0.0.1:9"                 # discard の口。誰も聞いていないので即座に断られる
_NO_LAB_ENV = {
    "LAPS_BACKEND": f"http://{_UNREACHABLE}/v1",
    "LAPS_RELAY_URL": f"ws://{_UNREACHABLE}",
    "LAPS_GATEWAY_TELEMETRY_URL": f"http://{_UNREACHABLE}/ingest",
}
os.environ.update(_NO_LAB_ENV)
# ★最初の下見（oc/loop._scout_first・2026-10-18）は試験では切る: 偽の推論機の台本を子が先に使ってしまう。
#   下見そのものを確かめる試験（test_explore_first_20261018）は自分で 1 にする。
# ★終わりの合図の道具 Done は 2026-10-21 から既定で渡さない（oc/loop._done_tool_on）。台本で Done を呼ぶ既存の試験のため、試験の既定では
#   渡す（1）。渡さない既定の振る舞いは test_no_done_20261021 が自分で 0 にして確かめる。
#   ★LAPS_SELF_REVIEW（2026-10-21）: 終える前の見直しは、ファイルを変えて終える試験のすべてに 1 手番を足すので試験では切る
#     （見直しそのものは test_self_review_20261021 が "1" にして確かめる）。
#   ★LAPS_SKILL_GATE（2026-11-16）: スキルの門は、20 字以上の依頼のすべてで最初の道具を止めるので試験では切る（門の試験は自分で入れる）
#   ★2026-11-16 本番では走行を止める上限を外した（オーナー「その他の制限も全部取り払って」）。仕組みの試験は今までの値で回す
#     （外した既定は test_no_limits_20261116 が自分で消して確かめる）。
_TEST_DEFAULTS = {"LAPS_EXPLORE_FIRST": "0", "LAPS_DONE_TOOL": "1", "LAPS_SELF_REVIEW": "0", "LAPS_SKILL_GATE": "0",
                  "LAPS_DOOM_LOOP": "1", "LAPS_LOOP_GUARD": "1", "LAPS_STUCK_GATE": "1", "LAPS_REPEAT_STOP": "1",
                  "LAPS_LOOK_STREAK": "15", "LAPS_MAX_EMPTY": "2", "LAPS_MAX_UNREAD": "3", "LAPS_CHILD_MAX_ITERS": "40"}
os.environ.update(_TEST_DEFAULTS)
# ★旧い名前（BLACKWALL_*）は試験のプロセスの環境に置かない（2026-11-14・環境変数の名前を LAPS_* に変えた）。
#   `laps` は import のときに新旧の名前を写し合う（laps/_envcompat.py）。旧い名前が残っていると、試験が子プロセスの
#   環境から LAPS_X を外しても、子の中で旧い名前から値が戻る（例: 新しい中核の試験が LAPS_ENGINE を外す）。
#   本体はもう LAPS_* しか読まないので、ここで 1 回落とせば足りる（試験ごとの復元 _restore_environ がこの状態へ戻す）。
from laps import _envcompat as _laps_envcompat  # noqa: E402  （この import で 1 回写される）
for _k in [k for k in os.environ if k.startswith(_laps_envcompat.OLD)]:
    del os.environ[_k]
_LAB_NET = ipaddress.ip_network("192.168.0.0/16")


def _is_lab(address) -> bool:
    try:
        return ipaddress.ip_address(address[0]) in _LAB_NET
    except (ValueError, TypeError, IndexError):
        return False


_real_connect = socket.socket.connect
_real_connect_ex = socket.socket.connect_ex


def _guarded_connect(self, address):
    if _is_lab(address):
        raise ConnectionRefusedError(f"試験からラボの LAN（{address[0]}）へはつながない（tests/conftest.py）")
    return _real_connect(self, address)


def _guarded_connect_ex(self, address):
    if _is_lab(address):
        return 111                            # ECONNREFUSED
    return _real_connect_ex(self, address)


socket.socket.connect = _guarded_connect
socket.socket.connect_ex = _guarded_connect_ex


@pytest.fixture(autouse=True)
def _no_lab_network(monkeypatch):
    """★上の ① を試験ごとに入れ直す（_restore_environ が戻すのは試験の始まりの値なので、途中で消した物も戻る）。"""
    for k, v in {**_NO_LAB_ENV, **_TEST_DEFAULTS}.items():
        monkeypatch.setenv(k, v)


# ★試験が作った子プロセスを、全体の終わりに必ず片付ける（2026-10-15）。
#   ラボで全体の試験を回したとき、test_long_run_records の serve が assert で落ちた後に残り、約 5 分ごとに
#   1 本ずつたまった（止めた時点で 5 本）。個々の試験も直したが、取りこぼしはここで拾う。
_SPAWNED: list = []
_real_popen_init = subprocess.Popen.__init__


def _tracked_popen_init(self, *a, **kw):
    _real_popen_init(self, *a, **kw)
    _SPAWNED.append(self)


subprocess.Popen.__init__ = _tracked_popen_init


def pytest_sessionfinish(session, exitstatus):
    for p in _SPAWNED:
        try:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5)
        except Exception:
            pass


# ★pytest-timeout の既定値（W8→W7 第2回・2026-09-04）: 未導入だった間、
#   `test_gateway.py` の `@pytest.mark.timeout(60) if hasattr(pytest.mark, "timeout")
#   else (lambda f: f)` という防御的な書き方が「マーカーはあるが無効」という形で
#   静かに機能していなかった（`pytest.mark.<any>` は未登録のマーカーでも
#   AttributeError にならず作れてしまうため、プラグイン不在に気づけない）。
#   preflight の全試験は、1本が固まると 25 分の外枠 timeout まで無言で待つ。
#
#   ここでは **CLI の `--timeout` / 環境変数 `PYTEST_TIMEOUT` / ini の `timeout`
#   のどれかが既に指定されていればそちらを尊重し**、何も指定が無いときだけ
#   既定 300 秒を入れる（個々の試験の `@pytest.mark.timeout(N)` は常にこれより
#   優先される＝pytest-timeout 自身の優先順位のまま）。
def pytest_configure(config):
    if not config.pluginmanager.hasplugin("timeout"):
        return  # pytest-timeout 未導入の環境では何もしない（壊さない）
    if getattr(config.option, "timeout", None) is not None:
        return
    if os.environ.get("PYTEST_TIMEOUT"):
        return
    if config.getini("timeout"):
        return
    config.option.timeout = 300


@pytest.fixture(autouse=True)
def _isolate_blackwall_home(tmp_path, monkeypatch):
    """★試験は本物の `~/.blackwall` に書かない（2026-09-02）。
    これが無かったので、`Agent()`/`Session()` を素で作る 20 ファイルの試験が開発機の
    `~/.blackwall/sessions` に**空の走行 dir を 1,210 本**残し、監査ログの 61% が試験由来に
    なっていた（8 領域監査）。試験が自分で `LAPS_HOME` を setenv する場合はそちらが勝つ
    （autouse はその前に走る）。"""
    monkeypatch.setenv("LAPS_HOME", str(tmp_path / "bwhome"))
    # ★本物のサインインにも触らない（2026-10-31 test_relay_index が 開発機 の ~/.config/blackwall/auth.json を消した）
    monkeypatch.setenv("LAPS_AUTH_FILE", str(tmp_path / "auth.json"))


@pytest.fixture(autouse=True)
def _no_startup_probe(monkeypatch):
    """★試験は網を叩かない。端末版の起動時の確認（`_startup_probe`）は、実在する推論機へ
    `/health` と `/v1/models` を聞きに行く。`_header()` を通る試験がそれを黙って踏むと、
    推論機の無い機械で 1 本ごとに 1 秒遅くなり、測定中の推論機にも触る。
    確認そのものを試す試験は、自分でこの変数を外す（test_term_startup_probe.py）。"""
    monkeypatch.setenv("LAPS_NO_STARTUP_PROBE", "1")


@pytest.fixture(autouse=True)
def _policy_workspace(monkeypatch):
    monkeypatch.setenv("LAPS_POLICY", "workspace")
    monkeypatch.setenv("LAPS_WORKSPACE", os.environ.get("TMPDIR", "/tmp"))


# ★空出力の「何回目か」はプロセス内の辞書で覚える（走行の間だけ覚える設計）。
#   試験は同じプロセスで何本も走るので、**試験ごとに初期化**しないと
#   別ファイルの試験が「2 回目」を見て落ちる（2026-08-29 に実際に落ちた）。
import pytest as _pytest


@_pytest.fixture(autouse=True)
def _reset_bash_empty_seen():
    try:
        from laps.tools import bash as _b
        _b._EMPTY_SEEN.clear()
    except Exception:
        pass
    yield


# ★AskUser の窓口（serve.py が差し込むプロセス全体のフック）を試験ごとに外す（2026-09-03）。
#   本物の serve._loop を回す試験が窓口を残し、後続の AskUser 試験が死んだ Server へ聞きに行って
#   300 秒待った（全体試験が 99% でハング・単体では緑）。本番側も走行の終わりで外すよう直したが、
#   試験の独立性はここでも守る。
@_pytest.fixture(autouse=True)
def _reset_ask_user_asker():
    yield
    try:
        from laps.tools import ask_user as _au
        _au.set_asker(None)
    except Exception:
        pass

@pytest.fixture(autouse=True)
def _reset_fold_repeat_memory():
    """★畳みの記憶（fold.fold_repeat のモジュール大域）を試験ごとに空にする（2026-09-03）。

    本番では serve が走行境界で reset するが、試験は 1 プロセスで数千本走るので、前の試験が
    同じ道具・同じ引数で得た出力が「前回と同じ出力…省きました」に畳まれて次の試験へ漏れる。
    実例: 0.1.182 の preflight で `SystemState memory` の新試験だけが落ちた（単独では緑）。"""
    try:
        from laps.tools import fold as _fold
        _fold.reset_repeat_memory()
        # ★2026-09-06 preflight 実測: Agent を実走する試験（test_agent_gate_budgets_reset）が
        #   畳みの通算回数 `_OUT_SEEN_N` を増やしたまま残し、後続の試験で
        #   注入の畳みが強くなって「2 件が 1 塊」になった（単独では緑・全体でだけ赤）。
        #   通算回数も試験ごとに 0 へ戻す。
        try:
            _fold._OUT_SEEN_N[0] = 0
        except Exception:
            pass
    except Exception:
        pass
    yield


@pytest.fixture(autouse=True)
def _restore_environ():
    """★試験ごとに os.environ を元に戻す（2026-09-06 preflight 実測）。

    Agent を実走する試験（test_agent_gate_budgets_reset）で**本番コードが**
    `LAPS_N_CTX=16384` と `LAPS_SESSION_ID` を os.environ に直書きし、
    monkeypatch を通らないので試験の後も残る。後続の試験では上限（cap）が
    16K 窓の値に縮み「2 件が 1 塊」になった（単独では緑・全体スイートでだけ赤＝順序依存）。
    monkeypatch.setenv は自分の分しか戻さないので、ここで丸ごと復元する。
    """
    saved = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(saved)


# ── クラウドで落ちる既知の試験（2026-10-15・DESIGN-improvements の 2-7） ─────────────────────────────
#   クラウドで回すと約 240 本が落ち、新しく壊れた物が埋もれていた。ラボにしか無い物（lab）は飛ばし、今の作りに無い物を
#   確かめている物（stale）は xfail にする。表は known_failures.tsv（理由つき）。ここに無くて落ちる物が本物。
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parents[2]
#: ラボの作業木か（根に scratch/ がある＝ラボの実物の置き場）。BW_LAB=1/0 で決め打ちできる。
_IN_LAB = (os.environ.get("BW_LAB") == "1") or (os.environ.get("BW_LAB") != "0" and (_REPO_ROOT / "scratch").is_dir())


def _known_failures() -> list[tuple[str, str, str]]:
    rows = []
    try:
        for ln in (_Path(__file__).with_name("known_failures.tsv")).read_text(encoding="utf-8").splitlines():
            if ln and not ln.startswith("#"):
                nid, kind, why = (ln.split("\t") + ["", ""])[:3]
                rows.append((nid, kind, why))
    except OSError:
        pass
    return rows


def pytest_collection_modifyitems(config, items):
    if os.environ.get("BW_KNOWN_FAILURES") == "0":          # 表を使わずに全部を素で回す
        return
    exact, prefix = {}, []
    for nid, kind, why in _known_failures():
        if "[" in nid and not nid.endswith("]"):
            prefix.append((nid, kind, why))
        else:
            exact[nid] = (kind, why)
    for item in items:
        hit = exact.get(item.nodeid)
        if hit is None:
            hit = next(((k, w) for n, k, w in prefix if item.nodeid.startswith(n)), None)
        if hit is None:
            continue
        kind, why = hit
        if kind == "lab" and not _IN_LAB:
            item.add_marker(pytest.mark.skip(reason=f"lab only: {why}"))
        elif kind == "stale":
            item.add_marker(pytest.mark.xfail(reason=f"stale: {why}", strict=False))


@pytest.fixture(autouse=True)
def _reset_cancel_probe():
    """★道具の「止められたか」の問い合わせ先（oc/tools._cancel_probe）を試験ごとに戻す（2026-10-18）。
    走行が自分の _is_cancelled をここへ差し込み、止めた走行の試験（test_oc_loop_audit の L9 など）の後も残っていたので、
    同じ process で後に走った Bash の試験が「Not run: the user stopped the task.」になった（ファイルの並び順で落ちた）。"""
    yield
    try:
        from laps.oc import tools as _oc_tools
        _oc_tools.set_cancel_probe(lambda: False)
    except Exception:
        pass
