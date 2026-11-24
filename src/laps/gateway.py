"""WebSocket ゲートウェイ — スマホ（Android 版）から 開発機 の `laps.serve` へ届く口（2026-09-03）。

★なぜ要るか: serve は stdin/stdout の NDJSON でしか話せない（Windows GUI は sidecar が、Linux 端末は
  client.py が、同じ機械で子プロセスとして抱える）。オーナーは「家にいる時間が無い」＝外出先の
  スマホから使いたい。WireGuard で LAN が広告済みなので、開発機 に**線の口**があれば届く。

設計（変えない）:
  - 接続 1 本 = serve 1 本（`python -m laps.serve` を子に起動）。
  - クライアント→serve: テキストフレーム 1 つ = 1 行（JSON の検証はしない・そのまま stdin へ）。
  - serve→クライアント: stdout の 1 行 = 1 テキストフレーム（**変換しない**＝serve の契約を 1 つに保つ。
    画面側の解析は GUI の sidecar.rs / 端末の render.py と同じ物差し）。
  - 切断: serve へ cancel → shutdown → 5 秒で kill（**PID を名指し**。名前撃ちはしない）。
  - HTTP: `GET /health` → {"ok":true,"version":...,"connections":n}／`GET /sessions` → 走行の一覧。
  - 認証は環境変数 `LAPS_GATEWAY_TOKEN` の値で行う。未設定なら起動時に生成して `deploy/gateway.token`（0600）へ書く。
    **空文字なら無効**（オーナー方針「認証不要」への口。既定は要求する＝作業場で任意のコマンドが走る口なので）。
  - 上限: `LAPS_GATEWAY_MAX` 本（既定 4）。作業場 `LAPS_GATEWAY_WORKSPACE`（既定 /root/bw-mobile）、
    記録 `LAPS_GATEWAY_HOME`（既定 /root/.blackwall-mobile・スマホの走行だけをここに）。

起動: `python -m laps.gateway --host 0.0.0.0 --port 18400`（systemd: deploy/blackwall-gateway.service）
"""
from __future__ import annotations

from . import paths as _lp
from laps import _envcompat
import argparse
import asyncio
import json
import os
import secrets
import signal
import sys
import re
import time
from collections import deque
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

# ★`websockets.datastructures` / `websockets.http11` は純粋なデータ構造で、
#   `_ws_serve` が避けている「起動時にだけ要る」重い依存ではない——が、出荷物
#   （pyembed）は websockets を同梱しないので、**ここでも import は遅らせる**
#   （モジュールの import だけで落ちると、出荷の検証が丸ごと止まる）。
def _http_types():
    from websockets.datastructures import Headers
    from websockets.http11 import Response
    return Headers, Response


def _ws_serve(*args, **kwargs):
    """`websockets` は**起動時にだけ**要る。モジュール import では読まない——出荷物（pyembed）の検証は
    同梱の依存だけで全モジュールを import するので、ここで読むと出荷が落ちる（2026-09-03 に実際に落ちた）。
    ゲートウェイは 開発機 の .venv で動かす物で、出荷物には依存を足さない。"""
    try:
        from websockets.asyncio.server import serve
    except Exception as e:
        raise SystemExit(f"websockets が要ります（.venv に pip install websockets）: {e!r}")
    return serve(*args, **kwargs)

DEFAULT_BACKEND = "http://127.0.0.1:8080/v1"
DEFAULT_MODEL = "default"
PIPE_LINE_LIMIT = 32 * 1024 * 1024     # serve の出力の 1 行の上限（asyncio の既定は 64KiB）
TOKEN_FILE = Path(__file__).resolve().parents[2] / "deploy" / "gateway.token"
WEB_DIR = Path(__file__).resolve().parent / "webui"
#: 静的配信で出してよい拡張子だけ。★「拡張子から Content-Type を引く」ではなく
#: 「この表に無い物は出さない」＝ webui/ に何かが紛れ込んでも（.py・.token・.log）
#: 線の向こうへは出て行かない。増やすときは、それを誰に見せてよいか考えてから増やす。
WEB_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/vnd.microsoft.icon",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".woff2": "font/woff2",
}


def _user_head(content: str, n: int = 120) -> str:
    """一覧の題に使う「利用者の文」の先頭。

    ★serve は依頼を `[アプリからの指示]\n…\n\n[利用者]\n<本文>` で包む（serve.py の run_spec）。
      先頭 120 字をそのまま出すと前置きしか入らず、画面の履歴が全部 "O"（`OS:` の切れ端）になった
      （2026-09-07・Web クライアントの撮影で発見）。`[利用者]` 以降を取り、無ければ全文の先頭。"""
    marker = "[利用者]\n"
    i = content.rfind(marker)
    body = content[i + len(marker):] if i >= 0 else content
    body = body.strip()
    return body[:n]


def _log(msg: str) -> None:
    sys.stderr.write(f"[gateway] {time.strftime('%H:%M:%S')} {msg}\n")
    sys.stderr.flush()


def _version() -> str:
    # ★アプリ（Windows）が渡す版を先に見る（remotehost._version と同じ・2026-10-11）。同梱の Python には dist-info が無く、
    #   term.__version__ は「0.0.0+dev」になる＝ゲートウェイ経由の走行記録が版を失っていた（ウェブ版から送った走行で見つけた）。
    v = (os.environ.get("LAPS_APP_VERSION") or "").strip()
    if v:
        return v
    try:
        from .term import __version__
        return str(__version__)
    except Exception:
        return "?"


def _tail_summary(ev: Path, size: int) -> dict:
    """一覧の 1 行に添える「どう終わったか」と所要（2026-10-10・スマホの一覧に ✓/✗ と時間を出す）。
    ★記録の頭の 1 行と、末尾 256KB の中の最後の結果（type=result）の行だけを読む（全部は読まない）。
    結果の行が無ければ（走っている・途中で切れた）何も足さない。"""
    try:
        with open(ev, "rb") as fh:
            head = fh.readline()
            fh.seek(max(0, size - 262144))
            tail = fh.read()
        t0 = float(json.loads(head).get("ts") or 0)
        for raw in reversed(tail.split(b"\n")):
            if b'"type": "result"' not in raw:
                continue
            o = json.loads(raw)
            p = o.get("payload") or {}
            out = {"outcome": str(p.get("outcome") or "")}
            t1 = float(o.get("ts") or 0)
            if t0 and t1 >= t0:
                out["secs"] = round(t1 - t0, 1)
            return out
    except Exception:  # noqa: BLE001 — 一覧は落とさない
        pass
    return {}


class GatewayBusy(Exception):
    """同時に動かせる session の数の上限に達している。"""


class Session:
    """serve 1 本と、その事象の控え。クライアントが切れても猶予のあいだ生きる。

    ★なぜ（2026-09-03・設計書の発見③）: 携帯は回線が切れる。切断で serve を止めると走行が死ぬ。
      全事象に `seq` があるので、控えを持てば `since_seq` より後を再送して続きを流せる。
      serve 自身は過去の行を再送する口を持たないので、控えはここで持つ（直近 4,000 行）。
    """

    def __init__(self, sid: str, proc) -> None:
        self.sid = sid
        self.proc = proc
        self.buffer: deque[tuple[int, str]] = deque(maxlen=4000)   # (seq, 行)
        self.last_seq = 0
        self.client = None                 # いま繋がっている接続（無ければ None）
        self.run_id = None                 # 直近の依頼 id（猶予切れの cancel に載せる・2026-09-08 監査）
        self.detached_at: float | None = None
        self.pump_task: asyncio.Task | None = None
        self.err_task: asyncio.Task | None = None
        self.reaper: asyncio.Task | None = None
        self.recorder = None               # gateway_record.RunRecorder（走行の record・2026-09-07）
        self.running = False               # 依頼を渡してから done が返るまで（猶予切れで走行を殺さない・2026-10-10）

    @staticmethod
    def seq_of(line: str) -> int:
        m = re.search(r'"seq":\s*(\d+)', line)
        return int(m.group(1)) if m else 0


class Gateway:
    def __init__(self, *, token: str | None, workspace: Path, home: Path, max_conn: int,
                 backend: str, model: str, policy: str, toolset: str, grace: float = 600.0) -> None:
        self.grace = grace                      # 切断後に serve を生かす秒数（再接続の猶予）
        self.sessions_live: dict[str, Session] = {}
        self.token = token                      # None = 無効（認証なし）
        self.workspace = workspace
        self.home = home
        self.max_conn = max_conn
        self.backend, self.model, self.policy, self.toolset = backend, model, policy, toolset
        self.live: dict[int, asyncio.subprocess.Process] = {}   # 接続 id → serve

    # ── HTTP（WebSocket 以外の GET）───────────────────────────────
    def process_request(self, connection, request):
        u = urlsplit(request.path)
        path = u.path
        q = parse_qs(u.query)
        if path == "/health":
            body = json.dumps({"ok": True, "version": _version(), "connections": len(self.sessions_live),
                               "auth": self.token is not None,
                               # ★画面（/web/）が在るかどうか。ブラウザから開く前に、
                               #   「404 なのは合言葉のせいか・そもそも置いていないのか」を分けるため。
                               "web": (WEB_DIR / "index.html").is_file()}).encode()
            return connection.respond(HTTPStatus.OK, body.decode())
        # ★静的な画面（/web/）だけは**合言葉の前**に置く（2026-09-07）。
        #   理由: ここで token を要求すると、ブラウザは HTML を取りに行く時点で 401 になり、
        #   **合言葉を入力する画面そのものが出せない**（鍵をかける対象は API と /ws のほう）。
        #   出て行くのは画面のコード（HTML/CSS/JS）だけで、走行の題も本文も含まない。
        #   ★下の `token` の門より前にあるので、ここが「何を返すか」は表（WEB_TYPES）と
        #   経路の正規化だけが決める——`..` で webui/ の外へ出られないことは
        #   tests/test_gateway_web_static.py が生の HTTP で確かめる。
        if path == "/web" or path.startswith("/web/"):
            return self.web_file(connection, path)
        # ★合言葉は /ws だけでなく走行の一覧・中身にも要る（画面担当の指摘: 題と本文が漏れる）。
        if self.token is not None:
            got = (q.get("token") or [""])[0] or request.headers.get("X-Blackwall-Token", "")
            if not secrets.compare_digest(got.encode("utf-8"), self.token.encode("utf-8")):   # str どうしは非 ASCII で TypeError
                _log(f"合言葉が違う要求を断った: {path}")
                return connection.respond(HTTPStatus.UNAUTHORIZED, "token required\n")
        if path == "/sessions":
            def _int(name: str, default: int) -> int:
                try:
                    return int((q.get(name) or [str(default)])[0])
                except ValueError:
                    return default
            rows = self.sessions(limit=_int("limit", 50), has_events=(q.get("has_events") or ["0"])[0] == "1",
                                 order=(q.get("order") or ["desc"])[0])
            return connection.respond(HTTPStatus.OK, json.dumps(rows, ensure_ascii=False))
        m = re.fullmatch(r"/sessions/([A-Za-z0-9_.\-]+)/events", path)
        if m:
            try:
                since = int((q.get("since_seq") or ["0"])[0]); limit = int((q.get("limit") or ["0"])[0])
            except ValueError:
                since, limit = 0, 0
            body = self.session_events(m.group(1), since_seq=since, limit=limit)
            if body is None:
                return connection.respond(HTTPStatus.NOT_FOUND, "no such session\n")
            return connection.respond(HTTPStatus.OK, body)
        if path == "/live":
            return connection.respond(HTTPStatus.OK, json.dumps(self.live_sessions(), ensure_ascii=False))
        if path != "/ws":
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if (q.get("follow") or [""])[0]:
            return None                         # 追いかける接続は serve を立てない（数の上限に数えない）
        if (q.get("ctl") or [""])[0] == "1":
            return None                         # 機械の操作の線（remotectl）も serve を立てない
        if (q.get("lab") or [""])[0] == "1":
            return None                         # 作業場の線（labhost・作業用のコンテナ だけが開く）も serve を立てない
        sid = (q.get("session") or [""])[0]
        if sid not in self.sessions_live and len(self.sessions_live) >= self.max_conn:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, f"busy: {self.max_conn} sessions\n")
        return None

    # ── 静的な画面（/web/）────────────────────────────────────────
    def web_file(self, connection, path: str):
        """`laps/webui/` の中身をそのまま返す。**合言葉は要らない**（process_request の注記を見よ）。

        ★安全側の作り: ①`%2e%2e` を先に戻してから見る（戻す前に判定すると素通りする）
          ②`..` を含む経路は resolve より前に断る ③resolve した結果が webui/ の中にあることを
          確かめる（symlink で外へ出る経路も、ここで落ちる）④表に無い拡張子は出さない。
        """
        rel = unquote(path[len("/web"):])
        if "\x00" in rel or "\\" in rel:
            return connection.respond(HTTPStatus.BAD_REQUEST, "bad path\n")
        rel = rel.lstrip("/")
        if rel == "" or rel.endswith("/"):
            rel += "index.html"
        if any(part in ("..", "") for part in rel.split("/")):
            # 空の部分（`//`）も断る: 正規化の仕方が実装ごとに違うので、そもそも受けない。
            return connection.respond(HTTPStatus.BAD_REQUEST, "bad path\n")
        base = WEB_DIR.resolve()
        try:
            target = (base / rel).resolve()
        except OSError:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        if target != base and base not in target.parents:
            _log(f"webui の外を指す要求を断った: {path}")
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        ctype = WEB_TYPES.get(target.suffix.lower())
        if ctype is None or not target.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        try:
            body = target.read_bytes()
        except OSError as e:
            _log(f"画面のファイルを読めない（{target.name}）: {e!r}")
            return connection.respond(HTTPStatus.INTERNAL_SERVER_ERROR, "cannot read\n")
        Headers, Response = _http_types()
        headers = Headers()
        headers["Content-Type"] = ctype
        headers["Content-Length"] = str(len(body))
        # ★控えを持たせない: 出し直した画面が「古いまま動いている」を作らないため
        #   （更新の経路で 2 度壊している）。この規模（数十 KB・LAN 内）で困らない。
        headers["Cache-Control"] = "no-cache"
        return Response(HTTPStatus.OK.value, "OK", headers, body)

    def sessions(self, limit: int = 50, *, has_events: bool = False, order: str = "desc") -> list[dict]:
        """スマホの走行の一覧。中身は読まない（id・更新時刻・最初の依頼 1 行・events の有無と大きさ）。
        `has_events=True` で記録の無い空の走行を除く（画面担当の要望: 一覧が空箱だらけになる）。"""
        out = []
        root = self.home / "sessions"
        if not root.exists():
            return out
        dirs = sorted((d for d in root.iterdir() if d.is_dir()), key=lambda d: d.name, reverse=(order != "asc"))
        for d in dirs:
            ev = d / "events.jsonl"
            ev_bytes = ev.stat().st_size if ev.exists() else 0
            if has_events and ev_bytes == 0:
                continue
            first = ""
            try:
                with open(d / "messages.jsonl", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        m = json.loads(line)
                        if m.get("role") == "user":
                            first = _user_head(str(m.get("content") or ""))
                            break
            except Exception:
                pass
            row = {"id": d.name, "mtime": int(d.stat().st_mtime), "first": first, "events_bytes": ev_bytes}
            if ev_bytes:
                row.update(_tail_summary(ev, ev_bytes))
            out.append(row)
            if len(out) >= max(1, limit):
                break
        return out

    def session_events(self, sid: str, *, since_seq: int = 0, limit: int = 0) -> str | None:
        """過去の走行の事象（`events.jsonl` の行）を NDJSON のまま返す。読み返し用（画面担当の要望 1）。
        ★実測 p50 15KB・p90 80KB・最大 590KB なので一括で足りる。`since_seq` より後だけ・`limit` 行まで。"""
        # ★id の検証は `session.is_valid_session_id` に一本化する（2026-09-05・r4c）。
        #   旧: ここだけ独自の正規表現で、`".."` や先頭 `.` を通していた（1 階層だけ上へ
        #   出られる読み取り）。書く側（`serve._owner_sink`）に同じ形の穴が空いていたのを
        #   直したので、読む側も同じ 1 本の判定に揃える。
        from .session import is_valid_session_id
        if not is_valid_session_id(sid):
            return None
        ev = self.home / "sessions" / sid / "events.jsonl"
        if not ev.exists():
            return None
        out: list[str] = []
        try:
            with open(ev, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    if since_seq and Session.seq_of(line) <= since_seq:
                        continue
                    out.append(line)
                    if limit and len(out) >= limit:
                        break
        except OSError:
            return None
        return "\n".join(out) + ("\n" if out else "")

    # ── serve の起動と中継 ───────────────────────────────────────
    def _env(self) -> dict:
        env = dict(os.environ)
        src = str(Path(__file__).resolve().parents[1])
        env.setdefault("LAPS_USER_PYTHONPATH", env.get("PYTHONPATH", ""))   # 道具に渡す環境では元へ戻す（oc/toolenv.child_env）
        env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys([src] + env.get("PYTHONPATH", "").split(os.pathsep))).strip(os.pathsep)
        env.update({
            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8",
            "LAPS_BACKEND": self.backend, "LAPS_MODEL": self.model,
            "LAPS_WORKSPACE": str(self.workspace), "LAPS_POLICY": self.policy,
            "LAPS_NATIVE_TOOLS": "1", "LAPS_TOOLSET": self.toolset,
            "LAPS_MAX_SECONDS": "0", "LAPS_MAX_TOOL_CALLS": "0",
            "LAPS_HOME": str(self.home),
            # ★走行の記録に「ゲートウェイで走った」と残す（2026-11-01）。同じ機械のアプリが、自分の走行と見分けて実況を追うため。
            "LAPS_RUN_VIA": "gateway",
        })
        _envcompat.apply(env)                # 旧い名前（BLACKWALL_*）にも同じ値を入れる（LAPS が勝つ）
        return env

    async def _send_replay(self, ses: Session, connection, since: int) -> int:
        """控えのうち since より新しい行を順に送り、**送っている間に serve が出した行も追いつくまで**送る。

        ★2026-09-08 監査（serve/gateway high）: 旧は再送の一覧を先に作って送っていたので、送っている
        （await している）間に serve が出した行は、控えには入るが `ses.client` がまだ None で線にも出ず、
        再送の一覧にも無い＝**次の再接続まで誰にも届かない**。seq で「送った所」を覚え、新しい行が
        無くなるまで回す。最後の判定と呼び出し側の `ses.client = connection` の間に await は無い。"""
        sent_upto = since
        n = 0
        while True:
            pending = [(seq, line) for seq, line in list(ses.buffer) if seq > sent_upto]
            if not pending:
                return n
            for seq, line in pending:
                await connection.send(line)
                sent_upto = max(sent_upto, seq)
                n += 1

    async def _get_or_spawn(self, sid: str) -> tuple[Session, bool]:
        """同じ session 名の接続が**同時に** 2 本来ても serve を 1 本しか立てない（sid ごとの鍵）。

        ★2026-09-08 監査（serve/gateway high）: 旧: `sessions_live.get(sid)` → `await self._spawn(sid)` の間に
        await があり、2 本目も「無い」と見て spawn していた＝serve が 2 本立ち、後勝ちで登録されなかった
        方が**孤児**（停止も記録もされない）になる。戻り値の bool は「この呼び出しで立てたか」。
        """
        locks = getattr(self, "_spawn_locks", None)
        if locks is None:
            locks = self._spawn_locks = {}
        lock = locks.setdefault(sid, asyncio.Lock())
        async with lock:
            ses = self.sessions_live.get(sid)
            if ses is not None and ses.proc.returncode is not None:
                await self._close_session(ses)
                ses = None
            if ses is None:
                # ★数の上限はここでも見る（2026-11-13: 握手のときにだけ見ていたので、新しい session を並べて繋ぐと、どれも
                #   「まだ空きがある」と見て全部 serve を立てていた。立てている途中の分も数える）。
                spawning = getattr(self, "_spawning", 0)
                if len(self.sessions_live) + spawning >= self.max_conn:
                    raise GatewayBusy(f"busy: {self.max_conn} sessions")
                self._spawning = spawning + 1
                try:
                    return await self._spawn(sid), True
                finally:
                    self._spawning -= 1
            return ses, False

    def _forget_lock(self, sid: str) -> None:
        """終わった session の鍵を捨てる（名前の違う session が来るたびに 1 つずつ残っていた）。使っている最中の鍵は捨てない。"""
        locks = getattr(self, "_spawn_locks", None) or {}
        lock = locks.get(sid)
        if lock is not None and not lock.locked():
            locks.pop(sid, None)

    async def _spawn(self, sid: str) -> Session:
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.home.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", "-m", "laps.serve",
            cwd=str(self.workspace), env=self._env(),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            # ★1 行の上限を上げる（既定は 64KiB）。道具の結果（64K 字まで）や大きな Write の中身を載せた事象は 1 行で 64KiB を
            #   越える。越えると readline が ValueError を出して読む係が死に、会話が何も進まなくなる（2026-11-13 のレビュー）。
            limit=PIPE_LINE_LIMIT,
            # ★Windows の機械（remotehost）で動くとき、窓を出さない（2026-10-08）。
            **({"creationflags": 0x08000000} if os.name == "nt" else {}))
        ses = Session(sid, proc)
        try:
            from . import gateway_record as _gr
            # peer は WS ハンドラ側（connection がある所）で後から入れる
            ses.recorder = _gr.RunRecorder(sid=sid, version=_version(), peer="",
                                           workspace=str(self.workspace),
                                           backend=self._env().get("LAPS_BACKEND", ""),
                                           model=self._env().get("LAPS_MODEL", ""))
        except Exception as e:
            _log(f"recorder を作れない: {e!r}")
        self.sessions_live[sid] = ses
        self.live[id(proc)] = proc

        async def pump_out() -> None:          # serve stdout → 控え → 繋がっていればクライアント（無変換）
            assert proc.stdout is not None
            while True:
                try:
                    raw = await proc.stdout.readline()
                except ValueError as e:        # 上限より長い 1 行。その行だけ捨てて読み続ける（読む係を死なせない）
                    _log(f"serve の出力の 1 行が長すぎたので捨てた: {e}")
                    continue
                if not raw:
                    break
                line = raw.decode("utf-8", "replace").rstrip("\n")
                seq = Session.seq_of(line)
                ses.last_seq = max(ses.last_seq, seq)
                if ses.running and ('"done"' in line or '"error"' in line):
                    try:
                        _o = json.loads(line)
                        # 走行の終わり＝**いま走っている依頼の** done、または依頼 id つきの error（serve の terminal と同じ）。
                        # ★2026-10-16 監査: 旧はどの id の done でも下ろしていた——「止める」で待ち行列ごと消した依頼の done や
                        #   前の依頼の遅れた done で、走っている依頼が「終わった」ことになり、猶予切れで殺されうる状態に戻っていた。
                        #   error で下ろすのは done が来ない失敗（rejected）だけ（inject_failed などは走行中の 1 行・done が後に来る）。
                        if (_o.get("id") and str(_o.get("id")) == str(ses.run_id)
                                and (_o.get("type") == "done"
                                     or (_o.get("type") == "error" and _o.get("kind") in ("rejected", "fatal")))):
                            ses.running = False
                    except Exception:
                        pass
                ses.buffer.append((seq, line))
                # ★走行の record（2026-09-07）: Web/Android 経路も実機と同じ形で telemetry に残す。
                try:
                    if ses.recorder is not None:
                        _rec = ses.recorder.feed(line)
                        if _rec:
                            from . import gateway_record as _gr
                            _gr.post_record(_rec, on_error=_log)
                except Exception as e:  # 記録で走行を止めない
                    _log(f"record 失敗: {e!r}")
                c = ses.client
                if c is not None:
                    try:
                        await c.send(line)
                    except Exception:
                        ses.client = None
            self.live.pop(id(proc), None)      # serve が終わった
            # ★子が死んでも誰にも伝わらなかった（2026-09-03 発見）。stdout が EOF になるだけで
            #   ここまでは黙って戻るだけ・接続はそのまま開いたまま。クライアントは新しい行を
            #   永久に待ち続ける——WS の ping/pong はゲートウェイ自身が答えるので線は「生きて見える」。
            #   「done が出ない＝画面が永久に考え中」の実例その1。serve の行そのものは偽造しない
            #   （契約は 1 つに保つ）——伝えるのは WS の close コード（クライアントは既に
            #   非 1000 の close を「切れた」として扱い自動で繋ぎ直す＝ServeGateway.kt:236）。
            rc = proc.returncode
            _log(f"session {sid}: serve pid={proc.pid} が終わった（returncode={rc}）")
            self.sessions_live.pop(sid, None)
            self._forget_lock(sid)
            if ses.reaper:
                ses.reaper.cancel()
                ses.reaper = None
            c = ses.client
            if c is not None:
                ses.client = None
                try:
                    await c.close(code=4001, reason=f"serve process exited (code={rc})")
                except Exception:
                    pass

        async def pump_err() -> None:          # serve stderr → ログ
            assert proc.stderr is not None
            while True:
                try:
                    raw = await proc.stderr.readline()
                except ValueError:
                    continue
                if not raw:
                    break
                _log(f"serve[{proc.pid}] {raw.decode('utf-8', 'replace').rstrip()}"[:300])

        ses.pump_task = asyncio.create_task(pump_out())
        ses.err_task = asyncio.create_task(pump_err())
        _log(f"session {sid}: serve pid={proc.pid}（session 数 {len(self.sessions_live)}）")
        return ses

    async def _reap_later(self, ses: Session) -> None:
        """猶予を過ぎても戻って来なければ serve を止める。戻ってきたら取り消される。

        ★走行中は止めない（2026-10-10 オーナー「私は止めていません」）: 会話を見ていた画面（スマホ）の接続が
          17:20 ごろ切れ、600 秒後の 17:30 にこの猶予切れが走行中の会話へ cancel と shutdown を送っていた。
          画面は「作業の途中で手番が終わり…」で止まって見え、要約済みの文脈も保存されずに失われた。
          走行が終わる（done が返る）まで待ち、終わってからもう一度猶予をおく（結果を見に戻る時間）。
          上限は LAPS_GATEWAY_RUN_MAX_SECS（既定 12 時間）。"""
        try:
            await asyncio.sleep(self.grace)
            try:
                cap = float(os.environ.get("LAPS_GATEWAY_RUN_MAX_SECS") or 43200)
            except ValueError:
                cap = 43200.0
            t0, waited = time.time(), False
            while (ses.client is None and ses.running and ses.proc.returncode is None
                   and time.time() - t0 < cap):
                if not waited:
                    _log(f"session {ses.sid}: 猶予 {self.grace:.0f} 秒を過ぎたが走行中なので止めない（終わるまで待つ）")
                    waited = True
                await asyncio.sleep(15)
            if waited and ses.client is None and ses.proc.returncode is None:
                await asyncio.sleep(self.grace)
        except asyncio.CancelledError:
            return
        if ses.client is None:
            _log(f"session {ses.sid}: 猶予 {self.grace:.0f} 秒を過ぎた → serve pid={ses.proc.pid} を止める")
            await self._close_session(ses)

    async def _close_session(self, ses: Session) -> None:
        self.sessions_live.pop(ses.sid, None)
        self._forget_lock(ses.sid)
        await self._shutdown(ses.proc, getattr(ses, "run_id", None))
        for t in (ses.pump_task, ses.err_task):
            if t:
                t.cancel()
        self.live.pop(id(ses.proc), None)

    # ── 走っている会話を追いかける（2026-10-10・Android とパソコンの画面のリンク）────────────────
    #   中核（serve）は走行中の事象を sessions/<id>/live.ndjson にも書き、inbox.ndjson の割り込み・中止・答えを拾う
    #   （serve.py の _live_begin）。パソコンのアプリが抱える中核も同じ置き場を使うので、アプリに待ち受けの口を
    #   作らずに、パソコンで走っている会話をスマホでリアルタイムに見て、割り込める。
    _FOLLOW_TYPES = ("inject", "cancel", "answer")

    def live_sessions(self, limit: int = 20) -> list[dict]:
        """写し（live.ndjson）がある会話。走っている物を先に。"""
        base = self.home / "sessions"
        rows = []
        try:
            dirs = sorted((d for d in base.iterdir() if d.is_dir()), key=lambda d: d.stat().st_mtime, reverse=True)[:200]
        except OSError:
            return []
        now = time.time()
        for d in dirs:
            f = d / "live.ndjson"
            try:
                st = f.stat()
            except OSError:
                continue
            age = now - st.st_mtime
            if age > 6 * 3600:
                continue
            first, last = {}, {}
            try:
                with open(f, "rb") as fh:
                    head = fh.read(16384).split(b"\n")
                    fh.seek(max(0, st.st_size - 8192))
                    tail = [x for x in fh.read().split(b"\n") if x.strip()]
                for raw in head:
                    try:
                        o = json.loads(raw)
                    except Exception:
                        continue
                    if o.get("kind") == "run_start":
                        first = o
                        break
                for raw in reversed(tail):
                    try:
                        last = json.loads(raw)
                        break
                    except Exception:
                        continue
            except OSError:
                continue
            ended = last.get("type") == "done"
            rows.append({"id": d.name, "running": (not ended) and age < 300, "ended": ended, "age": int(age),
                         "mtime": int(st.st_mtime), "prompt": str(first.get("prompt") or "")[:300],
                         "source": str(first.get("source") or ""), "last_seq": int(last.get("seq") or 0)})
            if len(rows) >= limit:
                break
        rows.sort(key=lambda r: (not r["running"], -r["mtime"]))
        return rows

    async def follow(self, connection, sid: str, since: int) -> None:
        """写しを追って送り（since_seq より後から）、来たフレームは受け箱へ書く（割り込み・中止・答えだけ）。
        写しが作り直されたら（次の走行）最初から送り直す。読むだけ・serve は立てない。"""
        from .session import is_valid_session_id
        if not is_valid_session_id(sid):
            await connection.close(code=4404, reason="no such session")
            return
        d = self.home / "sessions" / sid
        live, inbox = d / "live.ndjson", d / "inbox.ndjson"

        async def reader() -> None:
            async for msg in connection:
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                try:
                    obj = json.loads(msg)
                except ValueError:
                    continue
                t = obj.get("type") if isinstance(obj, dict) else None
                if t == "ping":
                    await connection.send(json.dumps({"v": 1, "seq": 0, "type": "pong", "id": obj.get("id")}))
                    continue
                if t not in self._FOLLOW_TYPES:
                    await connection.send(json.dumps({
                        "v": 1, "seq": 0, "type": "notice", "id": obj.get("id") if isinstance(obj, dict) else None,
                        "kind": "follow_limited",
                        "message": "この画面から送れるのは割り込み・中止・質問への答えだけです"}, ensure_ascii=False))
                    continue
                obj.setdefault("source", "android")
                try:
                    with open(inbox, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(obj, ensure_ascii=False) + "\n")
                except OSError as e:
                    _log(f"follow {sid}: 受け箱に書けない: {e!r}")

        rtask = asyncio.create_task(reader())
        pos, sent, sig = 0, since, b""
        _log(f"follow {sid}: since_seq={since}")
        try:
            while not rtask.done():
                try:
                    st = live.stat()
                    with open(live, "rb") as fh:
                        head = fh.read(160)
                        if sig and head[:len(sig)] != sig or st.st_size < pos:
                            pos, sent = 0, 0           # 次の走行で作り直された
                        if not sig or pos == 0:
                            sig = head
                        if st.st_size > pos:
                            fh.seek(pos)
                            chunk = fh.read(st.st_size - pos)
                        else:
                            chunk = b""
                except OSError:
                    chunk = b""
                cut = chunk.rfind(b"\n")
                if cut >= 0:
                    pos += cut + 1
                    for raw in chunk[:cut].split(b"\n"):
                        if not raw.strip():
                            continue
                        line = raw.decode("utf-8", "replace")
                        sq = Session.seq_of(line)
                        if sq and sq <= sent:
                            continue
                        await connection.send(line)
                        sent = max(sent, sq)
                await asyncio.sleep(0.15)
        except Exception as e:  # noqa: BLE001 — 切断など
            _log(f"follow {sid}: {type(e).__name__}: {e}")
        finally:
            rtask.cancel()

    async def handler(self, connection) -> None:
        q = parse_qs(urlsplit(connection.request.path).query)
        if (q.get("ctl") or [""])[0] == "1":
            from . import remotectl
            return await remotectl.serve(self, connection)
        follow_sid = (q.get("follow") or [""])[0]
        if follow_sid:
            try:
                fs = int((q.get("since_seq") or ["0"])[0] or 0)
            except ValueError:
                fs = 0
            return await self.follow(connection, follow_sid, fs)
        resumable = bool((q.get("session") or [""])[0])   # session を名乗らない接続は戻れない＝切断で即終了
        sid = (q.get("session") or [""])[0] or f"anon-{id(connection)}"
        try:
            since = int((q.get("since_seq") or ["0"])[0] or 0)
        except ValueError:
            since = 0
        try:
            ses, spawned = await self._get_or_spawn(sid)
        except GatewayBusy as e:
            _log(f"session {sid}: 断った（{e}）")
            await connection.close(code=1013, reason=str(e))   # 1013＝あとでもう一度
            return
        if not spawned:
            if ses.reaper:
                ses.reaper.cancel()
                ses.reaper = None
            if ses.client is not None:
                try:
                    await ses.client.close(code=4000, reason="別の接続に引き継ぎ")
                except Exception:
                    pass
            ses.detached_at = None
            # ★再送: since_seq より後の控えを、続きを流す前に出す
            buf = list(ses.buffer)
            # ★控え（直近 4,000 行）より前に切れていたら、その間の行は**もう無い**。
            #   `seq > since` だけで判定すると、無くなった行も黙って「取りこぼし無し」に見える
            #   （実際は buf の先頭より古い行が抜けている）。せめてログには残す
            #   （線の契約は変えない＝ここで偽の行を作らない）。画面側は `seqGaps` で気づける
            #   はずが**どの画面にも一度も表示されていない**（EventReducer.kt:101 で数えるだけ）。
            if since > 0 and buf and since < buf[0][0] - 1:
                _log(f"session {sid}: since_seq={since} は控えの先頭 seq={buf[0][0]} より古い"
                     f"→ {buf[0][0] - 1 - since} 行が埋まらない（控えの外）")
            n_sent = await self._send_replay(ses, connection, since)
            _log(f"session {sid}: 再接続 since_seq={since} → 再送 {n_sent} 行（控え {len(ses.buffer)}）")
        ses.client = connection   # ★_send_replay の最後の「新しい行なし」判定と同じ手番で繋ぐ（間に await が無い）
        proc = ses.proc
        try:
            async for msg in connection:        # クライアント → serve stdin（1 フレーム = 1 行・無検証）
                try:
                    if ses.recorder is not None and isinstance(msg, str):
                        if not ses.recorder.peer:
                            try:
                                ses.recorder.peer = str((connection.remote_address or ("", ""))[0])
                            except Exception:
                                pass
                        ses.recorder.note_client(json.loads(msg))
                        try:
                            _m = json.loads(msg)
                            if isinstance(_m, dict) and _m.get("type") in ("run", "send") and _m.get("id"):
                                ses.run_id = str(_m.get("id"))
                                ses.running = True
                        except Exception:
                            pass
                except Exception:
                    pass
                if isinstance(msg, bytes):
                    msg = msg.decode("utf-8", "replace")
                if proc.stdin is None or proc.returncode is not None:
                    break
                proc.stdin.write((msg.rstrip("\n") + "\n").encode("utf-8"))
                await proc.stdin.drain()
        except Exception as e:
            _log(f"session {sid}: {type(e).__name__}: {e}")
        finally:
            if ses.client is connection:
                ses.client = None
                ses.detached_at = time.time()
                if resumable and proc.returncode is None and sid in self.sessions_live:
                    ses.reaper = asyncio.create_task(self._reap_later(ses))
                    _log(f"切断 session {sid}: serve pid={proc.pid} は猶予 {self.grace:.0f} 秒生かす（since_seq で戻れる）")
                else:
                    await self._close_session(ses)

    async def _shutdown(self, proc, run_id: str | None = None) -> None:
        """cancel → shutdown → 5 秒待って PID を kill。名前撃ちはしない。

        ★2026-09-08 監査（serve/gateway high）: 旧は `{"type":"cancel"}` に id が無く、serve 側の
        受理が id を要求するため**行儀のよい cancel は必ず断られ**、5 秒後の kill に落ちていた
        （done も result も残らない）。直近の依頼 id を載せる。"""
        if proc.returncode is not None:
            return
        try:
            if proc.stdin is not None:
                # ★走行が無ければ cancel を送らない（2026-10-08: id の無い cancel に serve が bad_request を返し、
                #   機械を止めるたびにスマホへ「cancel には対象の id が要る」の赤い行が届いていた）。
                lines = ([json.dumps({"type": "cancel", "id": run_id})] if run_id else []) + ['{"type":"shutdown"}']
                for line in lines:
                    proc.stdin.write((line + "\n").encode())
                await proc.stdin.drain()
                proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.wait()


def resolve_token() -> str | None:
    """環境変数 → 無ければ生成して deploy/gateway.token へ。空文字は「認証なし」。"""
    if "LAPS_GATEWAY_TOKEN" in os.environ:
        v = os.environ["LAPS_GATEWAY_TOKEN"].strip()
        return v or None
    try:
        if TOKEN_FILE.exists():
            v = TOKEN_FILE.read_text(encoding="utf-8").strip()
            if v:
                return v
        TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        v = secrets.token_urlsafe(12)
        # 作る時点で 0600（書いてから chmod すると、その間だけ他の利用者に読める）
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(v + "\n")
        os.chmod(TOKEN_FILE, 0o600)
        _log(f"合言葉を生成して {TOKEN_FILE} に書いた")
        return v
    except Exception as e:
        _log(f"合言葉ファイルを扱えない（{e!r}）→ 生成した値をメモリだけで使う")
        return secrets.token_urlsafe(12)


def build(argv: list[str] | None = None) -> tuple[Gateway, argparse.Namespace]:
    ap = argparse.ArgumentParser(description="L.A.P.S WebSocket gateway")
    ap.add_argument("--host", default=os.environ.get("LAPS_GATEWAY_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LAPS_GATEWAY_PORT", "18400")))
    ap.add_argument("--workspace", default=os.environ.get("LAPS_GATEWAY_WORKSPACE", "/root/bw-mobile"))
    ap.add_argument("--home", default=os.environ.get("LAPS_GATEWAY_HOME") or str(_lp.gateway_home()))
    ap.add_argument("--max", type=int, default=int(os.environ.get("LAPS_GATEWAY_MAX", "4")))
    ap.add_argument("--grace", type=float, default=float(os.environ.get("LAPS_GATEWAY_GRACE", "600")),
                    help="切断後に serve を生かす秒数（再接続の猶予）")
    ap.add_argument("--backend", default=os.environ.get("LAPS_BACKEND", DEFAULT_BACKEND))
    ap.add_argument("--model", default=os.environ.get("LAPS_MODEL", DEFAULT_MODEL))
    ap.add_argument("--policy", default=os.environ.get("LAPS_POLICY", "trusted"))
    ap.add_argument("--toolset", default=os.environ.get("LAPS_TOOLSET", "full"))
    a = ap.parse_args(argv)
    gw = Gateway(token=resolve_token(), workspace=Path(a.workspace), home=Path(a.home), max_conn=a.max,
                 backend=a.backend, model=a.model, policy=a.policy, toolset=a.toolset, grace=a.grace)
    return gw, a


async def run(gw: Gateway, host: str, port: int) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    async with _ws_serve(gw.handler, host, port, process_request=gw.process_request,
                         max_size=8 * 1024 * 1024, ping_interval=20, ping_timeout=20):
        _log(f"listening ws://{host}:{port}/ws  auth={'on' if gw.token else 'OFF'}  workspace={gw.workspace}")
        await stop.wait()
    for ses in list(gw.sessions_live.values()):
        await gw._close_session(ses)


def main(argv: list[str] | None = None) -> int:
    gw, a = build(argv)
    try:
        asyncio.run(run(gw, a.host, a.port))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
