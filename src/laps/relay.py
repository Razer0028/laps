"""リレー — BLACKWALL が入っている全部の機械を、スマホから 1 か所で扱うための中継（2026-10-08・推論機 で動かす）。

オーナー「全てのデバイスにインストールされているセッションを Android から管理できるようにしたい」
「コマンドを使わずともデフォルトで」「インストールされているデバイスのホスト名でソートして分かりやすく」
「中継器は 160 で良いかと」。

つくり:
  - 各機械（Windows のアプリ・Linux 版・開発機 のゲートウェイ）は、起動すると**自分から**ここへ繋ぎに来て名乗る
    （laps/remotehost.py）。機械の側は受け口を開けない（防火壁・家の外の回線に左右されない）。
  - スマホはここだけを相手にする。`GET /devices` で機械の一覧（ホスト名の順）、`/d/<機械>/…` で
    その機械のゲートウェイ（laps/gateway.py と同じ口: /health・/sessions・/sessions/<id>/events・/ws）に届く。
    ★中継は中身を変えない（serve の行をそのまま運ぶ）。画面の解析は 開発機 に直接繋ぐときと同じ。

線（機械の側）:
  - 制御: WS `/_device/control?id=…` — 最初に {"op":"hello","info":{…}} を送る。中継からは
    {"op":"http","rid":n,"path":"/sessions?…"} と {"op":"open","cid":"…","path":"/ws?…"} が来る。
    答えは {"op":"http_reply","rid":n,"status":200,"body":"…"}・{"op":"open_failed","cid":…,"error":…}・
    途中の更新は {"op":"info","info":{…}}。
  - 中身: WS `/_device/data?id=…&cid=…` — スマホの /ws 1 本ごとに機械が 1 本開け、中継が両方を繋ぐ。
  - 会話の目録（1.5・2026-10-30 オーナー「全端末で 1 つの会話の一覧」・laps/convindex.py の 1 件の形）:
    機械 → {"op":"index","full":true|false,"items":[…],"removed":[sid…]}。中継は全機械ぶんを index.json に覚え、
    スマホへ `GET /conversations`（全機械を混ぜて新しい順）と WS `/events`（変わった会話と機械の出入りを流す）、
    機械へ {"op":"peers","items":[…]}（繋いだとき）と {"op":"peer_conv","item":…}（変わったとき）を配る（アプリの知らせと一覧）。

会話の中身の写し（1.8・2026-11-11 オーナー「アカウントごとのセッション管理も全部サーバー側で」「Claude Code の Remote Control と同じ仕組みでいい」）:
  - 中継が機械の会話の事象（events.jsonl の行）を seq の順に写して持つ（Journal）。機械が寝ていても、ほかの端末から中身を読める。
  - 写しは、機械の一覧で updated が変わった会話を中継が機械から引いて足す（機械の側は今の版のまま）。
  - 期限では消さない。機械の一覧から外れた会話は archived の印で残す。消すのはアプリから（/auth の conv_delete）。
  - 機械が寝ている会話へは送れない（Remote Control と同じ・オーナー 11-11）。

認証（1.6・2026-10-31 オーナー「自分のアカウントでログインしているデバイスならどこからでも見れるように」「利用にはアカウント
登録が必要に」「メールアドレスで登録の方が楽かも」「システムは自作する」）:
  - アカウントはメールアドレスとパスワード（state の accounts.json・0600・パスワードは scrypt・認証情報は sha256 だけを持つ）。
    確認メールは送らない（ラボにメールを送る仕組みが無い）。登録は既定で最初の 1 人だけ（`LAPS_RELAY_SIGNUP=open` で開く・`closed` で閉じる）。
  - 端末ごとに認証情報を 1 つ発行する（1 台ずつサインアウトさせられる）。機械はサインインした線で繋ぐとそのアカウントの物になる。
    コードの承認では「接続だけ」の認証情報も出せる（名乗るだけ・ほかの機械を見ない。作業場の 作業用のコンテナ のような機械に置く）。
  - `LAPS_RELAY_AUTH`: off＝認証なし（旧の動き）／optional＝切り替えの期間（既定）／required＝サインインが必須。
    optional の間、サインインしていない相手には今までどおり全部見せる（締め出さない）。サインインした相手には自分の機械と、
    まだ誰の物でもない機械だけを見せる。required では自分の機械だけ。
  - 本文のあるやり取りは WebSocket `/auth`（websockets の HTTP は GET しか受けない）。1 行の JSON に 1 行で答える。
  - ★TLS は同じ口（18400）で受ける（最初の 1 バイトで平文と分ける・防火壁と各端末の設定を変えずに済む）。証明書は state の
    tls.crt・tls.key。各端末は証明書そのものを固定する（laps/relay_cert.pem）。パスワードと認証情報は TLS か手元の線でしか受けない。
  - 旧の `LAPS_RELAY_TOKEN`（全員で 1 つの合言葉）と `LAPS_RELAY_DEVICE_KEY` も効く（どちらも本番では未使用）。

起動: `python3 relay.py --host 0.0.0.0 --port 18400`（推論機 の systemd: blackwall-relay.service）。
パスワードを忘れたとき: 推論機 で `python3 relay.py --reset-password <メールアドレス>`（手元の線でだけ受ける）。
★blackwall の他のモジュールを import しない（この 1 ファイルだけを 推論機 に置いて動かすため）。
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import signal
import ssl
import sys
import time
from http import HTTPStatus
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

# 旧い名前（BLACKWALL_RELAY_*）で設定されていても読む。★この 1 枚だけを配る機械（推論機）には laps のパッケージが無く、
# laps/_envcompat.py の写しが効かないので、ここで同じ事をする（LAPS が在ればそちらが勝つ）。
for _k, _v in list(os.environ.items()):
    if _k.startswith("BLACKWALL_") and "LAPS_" + _k[len("BLACKWALL_"):] not in os.environ:
        os.environ["LAPS_" + _k[len("BLACKWALL_"):]] = _v

VERSION = "1.8"
_ID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
#: API の道（ウェブ版の画面のファイルとして探さない物）
_API_PATHS = ("/health", "/devices", "/server/metrics", "/conversations", "/events", "/auth")
_LOOPBACK = ("127.0.0.1", "::1")

# ★ウェブ版（Remote・2026-10-10 オーナー「Blackwallのウェブ版を公開してほしいです。全てのセッションを管理できるように」）。
#   中継と同じ口（LAN の中だけ）から画面のファイルを配る。中身に秘密は無いので合言葉は要らない（API と線は従来どおり合言葉）。
#   ★この表に無い拡張子・この置き場の外は出さない（`..`・隠しファイル・.py を配らない）。
WEB_DIR = Path(__file__).resolve().parent / "webremote"
#: 推論機の機械の数え（同じ機械の bw-metrics）。スマホとウェブ版は中継にしか届かないので、ここで中継する（1.3・2026-10-18）。
METRICS_URL = (os.environ.get("LAPS_RELAY_METRICS") or "http://127.0.0.1:8742/v1/metrics").strip()
_WEB_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
              ".css": "text/css; charset=utf-8", ".woff2": "font/woff2", ".woff": "font/woff",
              ".svg": "image/svg+xml", ".png": "image/png", ".json": "application/json", ".webmanifest": "application/manifest+json"}
_WEB_NAME = re.compile(r"[A-Za-z0-9_.\-/]{1,160}")
SEEN_KEEP_SECS = 30 * 86400          # 一覧に「いまは繋がっていない」として残す期間
#: 会話の目録の 1 件で受け取る欄（それ以外は捨てる）と、1 台あたりの上限
CONV_KEYS = ("sid", "title", "first", "last", "status", "outcome", "updated", "source", "via", "ask", "secs", "quiet", "group")
# 中継が足す欄: archived（機械の一覧から外れ、中継の写しだけがある）・copy（写しを持っている）・device・online（1.8）
CONV_MAX_PER_DEVICE = 500
JOURNAL_MAX_CONVS = 20_000        # 写しを持つ会話の数の上限（機械ごと）。★期限では消さない決まりなので、普段は届かない大きさにしてある
BROADCAST_SEND_SECS = 5.0          # 知らせを 1 人に送る時間の上限（越えた相手は外す。繋ぎ直せば一覧から届く）
_SID_RE = re.compile(r"[A-Za-z0-9_.\-]{1,80}")


#: scrypt の強さ（OWASP の表の 32MiB の行）。中継の MemoryMax は 256M なので、同時 2 本（_hash_gate）まで。
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2 ** 15, 8, 3
SCRYPT_MAXMEM = 64 * 1024 * 1024
TOKEN_PREFIX = "bwd_"
MACHINE_SCOPE = "machine"            # 名乗るだけの認証情報（ほかの機械を見ない・操作しない）
TOKEN_KEEP_SECS = 365 * 86400        # 使われないまま 1 年で切れる（使っている端末は延びる）
TOKEN_SAVE_EVERY = 3600.0            # 「最後に使った時刻」をディスクへ書く間隔
PASSWORD_MIN, PASSWORD_MAX = 8, 256
_EMAIL_RE = re.compile(r"[^@\s]{1,64}@[^@\s.]+(\.[^@\s.]+)+")
FAIL_WINDOW, FAIL_MAX = 300.0, 10    # 5 分に 10 回失敗したら止める（住所ごと・メールアドレスごと）
SIGNUP_WINDOW, SIGNUP_MAX = 3600.0, 20
AUTH_MSG_MAX = 4096                  # /auth の 1 通の上限（パスワードより長い物は要らない）
AUTH_MSGS_PER_LINE = 30
PAIR_KEEP_SECS, PAIR_MAX = 600.0, 50 # 画面のない機械のコードの有効時間と、同時に待てる数
_PAIR_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"   # 読み違えやすい字（0 O 1 I L）を外す
FRONT_FIRST_SECS = 20.0              # 繋いだきり 1 バイトも送らない線を待つ長さ
#: 握手の持ち時間。★process_request ごと切られるので、機械を待つ _open_device（20 秒）と dev.http（30 秒）より長くする（既定の 10 秒では接続の切断になった）
OPEN_TIMEOUT = 45.0


def _log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def normalize_email(email) -> str:
    return str(email or "").strip().lower()


def _hash_password(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=SCRYPT_MAXMEM, dklen=32)


def _token_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class Accounts:
    """アカウントと端末ごとの認証情報（state の accounts.json・0600）。パスワードも認証情報も、元の値は持たない。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.accounts: dict[str, dict] = {}
        self.tokens: dict[str, dict] = {}      # 認証情報の sha256 → 1 件
        self._last_save = 0.0
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.accounts = raw.get("accounts") or {}
            self.tokens = raw.get("tokens") or {}
        except FileNotFoundError:
            pass
        except Exception as e:
            # ★読めないまま空で始めると、次の保存で全アカウントを上書きして消す。読めないなら起動しない（Restart で気づける）。
            raise SystemExit(f"accounts.json を読めません（{e!r}）。直すか退避してから起動してください") from None

    def save(self) -> None:
        self._last_save = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"accounts": self.accounts, "tokens": self.tokens}, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def by_email(self, email: str) -> tuple[str, dict] | None:
        for aid, acc in list(self.accounts.items()):     # verify は別の糸から呼ぶ
            if acc.get("email") == email:
                return aid, acc
        return None

    @staticmethod
    def _password_record(password: str) -> dict:
        salt = secrets.token_bytes(16)
        return {"s": salt.hex(), "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                "h": _hash_password(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P).hex()}

    @staticmethod
    def _password_ok(password: str, rec: dict) -> bool:
        got = _hash_password(password, bytes.fromhex(rec["s"]), int(rec["n"]), int(rec["r"]), int(rec["p"]))
        return hmac.compare_digest(got.hex(), rec["h"])

    def create(self, email: str, password: str) -> str:
        return self.add(email, self._password_record(password))

    def add(self, email: str, record: dict) -> str:
        """★書き換えと保存はイベントループの糸だけで行う（重い計算だけ別の糸）。同じ .tmp へ 2 つの糸が書くと accounts.json が壊れる。"""
        aid = "a_" + secrets.token_hex(8)
        self.accounts[aid] = {"email": email, "pw": record, "created": int(time.time())}
        self.save()
        return aid

    def verify(self, email: str, password: str) -> str | None:
        hit = self.by_email(email)
        if hit is None:
            # 無いメールアドレスでも同じだけ時間をかける（有無を時間で言わない）
            self._password_record(password)
            return None
        return hit[0] if self._password_ok(password, hit[1].get("pw") or {}) else None

    def set_record(self, aid: str, record: dict) -> None:
        self.accounts[aid]["pw"] = record
        self.save()

    def password_matches(self, aid: str, password: str) -> bool:
        return self._password_ok(password, self.accounts[aid].get("pw") or {})

    def issue(self, aid: str, device: dict | None, scope: str = "") -> tuple[str, dict]:
        raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
        d = device if isinstance(device, dict) else {}
        now = int(time.time())
        rec = {"id": "t_" + secrets.token_hex(8), "aid": aid, "name": str(d.get("name") or "")[:80],
               "kind": str(d.get("kind") or "")[:20], "created": now, "last_used": now}
        if scope == MACHINE_SCOPE:
            rec["scope"] = MACHINE_SCOPE
        self.tokens[_token_key(raw)] = rec
        self.save()
        return raw, rec

    def check(self, raw: str) -> dict | None:
        key = _token_key(raw)
        rec = self.tokens.get(key)
        if rec is None or rec.get("aid") not in self.accounts:
            return None
        now = time.time()
        if now - float(rec.get("last_used") or 0) > TOKEN_KEEP_SECS:
            self.tokens.pop(key, None)
            self.save()
            return None
        rec["last_used"] = int(now)
        if now - self._last_save > TOKEN_SAVE_EVERY:
            self.save()
        return rec

    def of_account(self, aid: str) -> list[dict]:
        return [r for r in self.tokens.values() if r.get("aid") == aid]

    def drop_token(self, aid: str, tid: str) -> bool:
        for key, rec in list(self.tokens.items()):
            if rec.get("aid") == aid and rec.get("id") == tid:
                self.tokens.pop(key, None)
                self.save()
                return True
        return False

    def delete_account(self, aid: str) -> list[str]:
        """アカウントと、その認証情報を消す。消した認証情報の id を返す（生きている線を閉じるため）。"""
        gone = [r["id"] for r in self.of_account(aid)]
        self.tokens = {k: r for k, r in self.tokens.items() if r.get("aid") != aid}
        self.accounts.pop(aid, None)
        self.save()
        return gone


JOURNAL_MAX_BYTES = 64 * 1024 * 1024     # 1 会話の写しの上限（開発機 の 589 件の実測で最大 2.9MB・中央値 18KB）
_SEQ_RE = re.compile(r'"seq":\s*(\d+)')


def _safe_name(x: str) -> bool:
    """写しの置き場の名前に使えるか（★`..` や先頭の `.` は通さない＝置き場の外へ出さない）。"""
    return bool(_SID_RE.fullmatch(x or "")) and not x.startswith(".")


class Journal:
    """会話の中身の写し（1.8・2026-11-11 オーナー「アカウントごとのセッション管理も全部サーバー側で情報を管理して、
    どのデバイスでも同じアカウントでログインすれば自分のアカウントの他のセッションで自由に作業できるように」）。

    機械の events.jsonl の行をそのまま seq の順に持つ＝機械が寝ていても、ほかの端末から中身を読める。
    中継が機械から引いて足す（機械の側は今の版のまま・`/sessions/<会話>/events?since_seq=`）。
    ★期限では消さない。消すのはアプリから（オーナー 11-11）。消した会話は gone に残し、機械から来ても戻さない。
    置き場: <state>/journal/<機械>/<会話>.jsonl と meta.json（機械 → 会話 → {seq, updated, bytes, full}）。"""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.meta_file = root / "meta.json"
        try:
            raw = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        raw = raw if isinstance(raw, dict) else {}
        self.meta: dict[str, dict[str, dict]] = raw["meta"] if isinstance(raw.get("meta"), dict) else {}
        self.gone: dict[str, list[str]] = raw["gone"] if isinstance(raw.get("gone"), dict) else {}
        self._dirty = False

    def path(self, did: str, sid: str) -> Path | None:
        if not (_safe_name(did) and _safe_name(sid)):
            return None
        return self.root / did / f"{sid}.jsonl"

    def info(self, did: str, sid: str) -> dict | None:
        return (self.meta.get(did) or {}).get(sid)

    def is_gone(self, did: str, sid: str) -> bool:
        return sid in (self.gone.get(did) or [])

    def has_copy(self, did: str, sid: str) -> bool:
        """中身を 1 行でも持っているか（中身の無い会話の印だけの物は数えない）。"""
        return int((self.info(did, sid) or {}).get("bytes") or 0) > 0

    def append(self, did: str, sid: str, body: str, updated=None) -> int:
        """機械から来た行を足す。持っている seq より後の行だけ（引き直しても重ならない）。足した行の数を返す。"""
        p = self.path(did, sid)
        if p is None or self.is_gone(did, sid):
            return 0
        per = self.meta.setdefault(did, {})
        if sid not in per and len(per) >= JOURNAL_MAX_CONVS:
            return 0                             # 1 台が際限なく会話を名乗って、目録（meta.json）を太らせない
        m = per.setdefault(sid, {"seq": 0, "updated": 0, "bytes": 0})
        last = int(m.get("seq") or 0)
        out: list[str] = []
        for line in (body or "").splitlines():
            line = line.strip()
            mm = _SEQ_RE.search(line) if line else None
            s = int(mm.group(1)) if mm else 0
            if s <= last:
                continue
            out.append(line)
            last = s
        if out:
            data = ("\n".join(out) + "\n").encode("utf-8")
            if int(m.get("bytes") or 0) + len(data) > JOURNAL_MAX_BYTES:
                m["full"] = True                 # 上限を超えた分は持たない（元の機械には残っている）
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                with open(p, "ab") as fh:
                    fh.write(data)
                m["bytes"] = int(m.get("bytes") or 0) + len(data)
            m["seq"] = last
        if updated is not None:
            m["updated"] = updated
        self._save_soon()
        return len(out)

    def read(self, did: str, sid: str, since_seq: int = 0, limit: int = 0) -> str | None:
        """写しを NDJSON で返す（機械の `/sessions/<会話>/events` と同じ形）。無ければ None。"""
        p = self.path(did, sid)
        if p is None or self.is_gone(did, sid) or not p.is_file():
            return None
        out: list[str] = []
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.rstrip("\n")
                if not line:
                    continue
                if since_seq:
                    mm = _SEQ_RE.search(line)
                    if mm and int(mm.group(1)) <= since_seq:
                        continue
                out.append(line)
                if limit and len(out) >= limit:
                    break
        return "\n".join(out) + ("\n" if out else "")

    def delete(self, did: str, sid: str) -> bool:
        """写しを消し、同じ会話が機械から来ても戻さない印を残す。"""
        p = self.path(did, sid)
        if p is None:
            return False
        had = (self.meta.get(did) or {}).pop(sid, None) is not None or p.is_file()
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        g = self.gone.setdefault(did, [])
        if sid not in g:
            g.append(sid)
        self._save_soon()
        return had

    def set_aside(self, did: str) -> bool:
        """この機械の写しを、だれからも引けない所へ避ける（消さない）。置き場の名前は先頭が `.`＝機械の名札としては通らない。
        ★「消した会話」の印（gone）は残す。外すと、機械を一覧から外して繋ぎ直しただけで、アプリから消した会話が機械から戻ってくる。"""
        had = self.meta.pop(did, None) is not None
        if _safe_name(did):
            d = self.root / did
            try:
                if d.is_dir():
                    os.replace(d, self.root / f".aside-{did}-{int(time.time())}")
                    had = True
            except OSError as e:
                _log(f"写しを避けられない {did}: {e!r}")
        if had:
            self._save_soon()
        return had

    def _save_soon(self) -> None:
        if self._dirty:
            return
        self._dirty = True
        try:
            asyncio.get_running_loop().call_later(2.0, self.save)
        except RuntimeError:
            self.save()

    def save(self) -> None:
        self._dirty = False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.meta_file.with_suffix(".tmp")
            tmp.write_text(json.dumps({"meta": self.meta, "gone": self.gone}, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.meta_file)
        except Exception as e:
            _log(f"写しの目録を保存できない: {e!r}")


class Device:
    def __init__(self, did: str, info: dict, ws, who: dict | None = None) -> None:
        self.id = did
        self.info = info
        self.ws = ws
        self.who = who                     # この機械を繋いだアカウント（サインインしていない線は None）
        self.since = time.time()
        self.pending: dict[int, asyncio.Future] = {}
        self.rid = 0

    async def send(self, obj: dict) -> None:
        await self.ws.send(json.dumps(obj, ensure_ascii=False))

    async def http(self, path: str, timeout: float = 30.0) -> tuple[int, str]:
        self.rid += 1
        rid = self.rid
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            await self.send({"op": "http", "rid": rid, "path": path})
            r = await asyncio.wait_for(fut, timeout)
            return int(r.get("status") or 502), str(r.get("body") or "")
        finally:
            self.pending.pop(rid, None)


class Relay:
    def __init__(self, *, token: str | None, device_key: str | None, state_dir: Path,
                 auth_mode: str = "off", signup: str = "open") -> None:
        self.token = token
        self.device_key = device_key
        self.auth_mode = auth_mode
        self.signup = signup
        self.accounts = Accounts(state_dir / "accounts.json") if auth_mode != "off" else None
        self.tls_on = False                  # 同じ口で TLS を受けているか（run が決める）
        self.front_peers: dict[tuple[int, int], str] = {}  # 手前の口から内側の口へ繋いだ線の（手元の番号, 内側の口）→ 本当の相手の住所
        self.fails: dict[str, list[float]] = {}
        self.pairing: dict[str, dict] = {}   # 画面のない機械のサインインの待ち（内緒のコード → 1 件）
        self.live: dict[str, set] = {}       # 認証情報の id → その認証情報で開いている線（サインアウトで閉じる）
        self._hash_gate = asyncio.Semaphore(2)
        self.devices: dict[str, Device] = {}
        self.waiters: dict[str, asyncio.Future] = {}
        # ★スマホの /ws は、機械の側の線が開いてから受け入れる（1.1）。開いた線を握手の終わりまで預かる（鍵は接続の物・1.4）。
        self.preopened: dict = {}
        self.state_file = state_dir / "devices.json"
        self.seen: dict[str, dict] = {}
        try:
            self.seen = json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            self.seen = {}
        # 会話の目録（機械 → sid → 1 件）。機械が繋がっていない間も覚えておく（一覧に「オフライン」で出す）。
        self.index_file = state_dir / "index.json"
        self.index: dict[str, dict[str, dict]] = {}
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
            self.index = {d: v for d, v in raw.items() if isinstance(v, dict)} if isinstance(raw, dict) else {}
        except Exception:
            self.index = {}
        self._index_dirty = False
        self.subscribers: dict = {}          # スマホ・ウェブ版の /events（線 → 相手）
        # 会話の中身の写し（1.8）。機械ごとに「引きに行く会話」の列と、引く係を 1 本ずつ持つ（機械に一度に何本も頼まない）。
        self.journal = Journal(state_dir / "journal")
        self.sync_want: dict[str, dict[str, object]] = {}    # 機械 → 会話 → 一覧の updated
        self.sync_task: dict[str, asyncio.Task] = {}

    def _signup_state(self) -> str:
        """登録できるか。first は最初の 1 人だけ（★開いたままだと LAN の誰でもアカウントを作り、持ち主のいない機械を自分の物にできる）。"""
        if self.signup == "first":
            return "closed" if self.accounts is not None and self.accounts.accounts else "open"
        return self.signup

    # ── だれに何を見せるか ────────────────────────────────────────
    def _can_see(self, who: dict | None, did: str) -> bool:
        """この相手がこの機械（と、その会話）を見てよいか。★見せる判定はここ 1 か所。"""
        owner = (self.seen.get(did) or {}).get("owner")
        if who is not None and who.get("scope") == MACHINE_SCOPE:
            return False                             # 接続だけの認証情報には何も見せない（自分の会話の目録も配らない）
        if who is None:
            return self.auth_mode != "required"      # 切り替えの期間: サインインしていない相手には今までどおり見せる
        if who.get("aid") == "*":                     # 旧の全員で 1 つの合言葉
            return True
        if owner is None:
            return self.auth_mode != "required"      # まだ誰の物でもない機械（切り替えの期間だけ見せる）
        return owner == who.get("aid")

    def _set_aside(self, did: str, why: str) -> None:
        """この機械の目録と写しを、だれにも見えない所へ避ける（消さない）。"""
        had = self.index.pop(did, None) is not None
        self.sync_want.pop(did, None)
        t = self.sync_task.pop(did, None)
        if t is not None and not t.done():
            t.cancel()
        had = self.journal.set_aside(did) or had
        if had:
            self._save_index_soon()
            _log(f"目録と写しを避けた: {did}（{why}）")

    # ── 一覧 ──────────────────────────────────────────────────────
    def _remember(self, did: str, info: dict, owner: str | None = None) -> None:
        # ★持ち主は上書きしない（名乗り直しや切断の記録で消さない）
        old = self.seen.get(did) or {}
        keep = owner or old.get("owner")
        # ★前の持ち主（was_owner）: サインアウトやアカウントの削除で持ち主がいなくなった機械に、別のアカウントが同じ名札で
        #   繋ぐと、前の持ち主の会話の目録と写しがそのまま見えていた（2026-11-13 のレビュー）。持ち主が替わったら避ける。
        was = old.get("was_owner")
        if owner and was and was != owner:
            self._set_aside(did, "持ち主が替わった")
            was = None
        if owner:
            was = None                           # 同じアカウントが戻った＝そのまま使う
        self.seen[did] = {**info, "last_seen": int(time.time()), **({"owner": keep} if keep else {}),
                          **({"was_owner": was} if was else {})}
        self._save_seen()

    def _save_seen(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.seen, ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.state_file)
        except Exception as e:
            _log(f"一覧を保存できない: {e!r}")

    def device_list(self, who: dict | None = None) -> list[dict]:
        now = time.time()
        rows = []
        for did, info in self.seen.items():
            dev = self.devices.get(did)
            if dev is None and now - float(info.get("last_seen") or 0) > SEEN_KEEP_SECS:
                continue
            if not self._can_see(who, did):
                continue
            row = dict(dev.info if dev else info)
            row.pop("owner", None)
            row["id"] = did
            row["online"] = dev is not None
            row["since"] = int(dev.since) if dev else None
            row["last_seen"] = int(now) if dev else info.get("last_seen")
            if who is not None:
                row["mine"] = info.get("owner") == who.get("aid")
            rows.append(row)
        # ★ホスト名の順（オーナー「ホスト名でソートして分かりやすく」）。同じ名前は種類で分ける。
        rows.sort(key=lambda r: (str(r.get("name") or r["id"]).casefold(), str(r.get("kind") or ""), r["id"]))
        return rows

    # ── 会話の目録 ────────────────────────────────────────────────
    def _clean_item(self, it) -> dict | None:
        if not isinstance(it, dict) or not _SID_RE.fullmatch(str(it.get("sid") or "")):
            return None
        out = {k: it[k] for k in CONV_KEYS if k in it}
        for k in ("title", "first", "last", "ask"):
            if k in out:
                out[k] = str(out[k])[:400]
        if "group" in out:                     # 会話のグループ（2026-11-19・アプリで名付けた物）
            out["group"] = str(out["group"])[:60]
        # ★並べ替えに使う欄は数にそろえる（2026-11-13: updated に null や文字列が 1 件入ると、一覧の sort が TypeError で落ち、
        #   その機械を見られる全員の /conversations が 500 になる。index.json に残るので起動し直しても続く）。
        for k in ("updated", "secs", "quiet"):
            if k in out:
                try:
                    v = float(out[k])
                    out[k] = (int(v) if v == int(v) else v) if v == v and abs(v) != float("inf") else 0
                except (TypeError, ValueError, OverflowError):
                    out[k] = 0
        for k in ("status", "outcome", "source", "via"):
            if k in out:
                out[k] = str(out[k])[:64]
        return out

    def _with_device(self, did: str, it: dict) -> dict:
        info = (self.devices[did].info if did in self.devices else self.seen.get(did)) or {}
        return {**it, "device": did, "device_name": info.get("name") or did, "device_kind": info.get("kind") or "",
                "online": did in self.devices, "copy": self.journal.has_copy(did, str(it.get("sid") or ""))}

    def apply_index(self, did: str, msg: dict) -> list[dict]:
        """機械から来た目録を覚える。変わった会話（機械の名前つき）を返す。"""
        cur = self.index.setdefault(did, {})
        changed = []

        def drop(sid: str) -> bool:
            """機械の一覧から外れた会話。★中継に写しがあれば消さずに残す（期限で消さない・オーナー 11-11）＝機械に無いだけの印。"""
            it = cur.get(sid)
            if it is None:
                return False
            if self.journal.has_copy(did, sid):
                if it.get("archived"):
                    return False
                cur[sid] = {**it, "archived": True, "status": "idle"}
                cur[sid].pop("ask", None)
                changed.append(self._with_device(did, cur[sid]))
                return False
            cur.pop(sid, None)
            return True

        if msg.get("full"):
            keep = {str(x.get("sid")) for x in (msg.get("items") or []) if isinstance(x, dict)}
            for sid in [s for s in cur if s not in keep]:
                drop(sid)
        for it in msg.get("items") or []:
            c = self._clean_item(it)
            if c is None or self.journal.is_gone(did, c["sid"]):
                continue                         # アプリから消した会話は機械から来ても戻さない
            if cur.get(c["sid"]) != c:
                cur[c["sid"]] = c
                changed.append(self._with_device(did, c))
        for sid in msg.get("removed") or []:
            if drop(str(sid)):
                changed.append({"sid": str(sid), "device": did, "removed": True})
        live = [kv for kv in cur.items() if not kv[1].get("archived")]
        if len(live) > CONV_MAX_PER_DEVICE:
            for sid, _ in sorted(live, key=lambda kv: kv[1].get("updated", 0))[:len(live) - CONV_MAX_PER_DEVICE]:
                drop(sid)
        if changed or msg.get("full"):
            self._save_index_soon()
        return changed

    # ── 写しを機械から引く（1.8）────────────────────────────────
    def want_sync(self, did: str, items) -> None:
        """機械の一覧のうち、写しより新しい会話を引く列に入れる（機械が繋いでいる間だけ動く）。"""
        want = self.sync_want.setdefault(did, {})
        for it in items or []:
            c = self._clean_item(it)
            if c is None or self.journal.is_gone(did, c["sid"]) or not _safe_name(c["sid"]):
                continue
            j = self.journal.info(did, c["sid"]) or {}
            if j.get("updated") != c.get("updated") or j.get("updated") is None:
                if c["sid"] not in want and len(want) >= CONV_MAX_PER_DEVICE:
                    continue                     # 目録と同じ上限（1 台が際限なく会話を名乗って、列と写しを太らせない）
                want[c["sid"]] = c.get("updated")
        t = self.sync_task.get(did)
        if want and did in self.devices and (t is None or t.done()):
            self.sync_task[did] = asyncio.get_running_loop().create_task(self._sync_loop(did))

    async def _sync_loop(self, did: str) -> None:
        want = self.sync_want.get(did) or {}
        while want and did in self.devices:
            # ★新しい物から（いま触っている会話が先に読めるように）
            sid = max(want, key=lambda s: (want[s] or 0) if isinstance(want[s], (int, float)) else 0)
            updated = want.pop(sid)
            try:
                await self.sync_one(did, sid, updated)
            except Exception as e:
                _log(f"写しを引けない {did}/{sid}: {type(e).__name__}: {e}")
            await asyncio.sleep(0.05)

    async def sync_one(self, did: str, sid: str, updated=None, timeout: float = 30.0) -> bool:
        """1 つの会話の続きを機械から引いて足す。引けたら True。"""
        dev = self.devices.get(did)
        if dev is None or self.journal.is_gone(did, sid) or not _safe_name(sid):
            return False
        last = int((self.journal.info(did, sid) or {}).get("seq") or 0)
        status, body = await dev.http(f"/sessions/{sid}/events?since_seq={last}", timeout=timeout)
        if status == 200:
            self.journal.append(did, sid, body, updated=updated)
            return True
        if status == 404 and updated is not None:
            # 中身の無い会話（道具なしの会話など）。同じ updated の間は引き直さない。
            self.journal.meta.setdefault(did, {}).setdefault(sid, {"seq": 0, "updated": 0, "bytes": 0, "empty": True})["updated"] = updated
            self.journal._save_soon()
        return False

    def flush(self) -> None:
        """止まる前に、2 秒後に書くつもりだった目録を書く（2026-11-13: 止める合図がその 2 秒の間に来ると、写しの seq が古いまま残り、
        起動し直した後に同じ行をもう一度足していた）。"""
        try:
            if self._index_dirty:
                self._save_index()
            if self.journal._dirty:
                self.journal.save()
        except Exception as e:
            _log(f"止まる前の保存に失敗: {e!r}")

    def _save_index_soon(self) -> None:
        if self._index_dirty:
            return
        self._index_dirty = True
        try:
            asyncio.get_running_loop().call_later(2.0, self._save_index)
        except RuntimeError:
            self._save_index()

    def _save_index(self) -> None:
        self._index_dirty = False
        try:
            self.index_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.index_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.index, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.index_file)
        except Exception as e:
            _log(f"目録を保存できない: {e!r}")

    def conversations(self, *, limit: int = 200, q: str = "", active: bool = False, device: str = "",
                      who: dict | None = None) -> list[dict]:
        """全機械の会話を混ぜて新しい順。30 日見ていない機械の分は出さない（devices と同じ）。"""
        now = time.time()
        rows = []
        qf = q.casefold().strip()
        for did, items in self.index.items():
            if device and did != device:
                continue
            if not self._can_see(who, did):
                continue
            seen = self.seen.get(did) or {}
            stale = did not in self.devices and now - float(seen.get("last_seen") or 0) > SEEN_KEEP_SECS
            for it in items.values():
                if stale and not self.journal.has_copy(did, str(it.get("sid") or "")):
                    continue                     # ★中継に写しのある会話は、機械を長く見ていなくても出す（1.8・期限で消さない）
                if active and it.get("status") not in ("running", "asking"):
                    continue
                if qf and qf not in (str(it.get("title") or "") + " " + str(it.get("first") or "")).casefold():
                    continue
                rows.append(self._with_device(did, it))
        rows.sort(key=lambda r: r.get("updated", 0), reverse=True)
        return rows[:max(1, limit)]

    async def _broadcast(self, phone_msg: dict, device_msg: dict | None = None, *, did: str = "") -> None:
        """知らせを配る。did（どの機械の話か）を見てよい相手にだけ。"""
        raw = json.dumps(phone_msg, ensure_ascii=False)
        for ws, who in list(self.subscribers.items()):
            if did and not self._can_see(who, did):
                continue
            try:
                # ★1 人ずつ待つので、窓の詰まったスマホが 1 台いると、その機械の http_reply も読めずに操作が時間切れになる（2026-11-13）。
                await asyncio.wait_for(ws.send(raw), BROADCAST_SEND_SECS)
            except asyncio.TimeoutError:
                self.subscribers.pop(ws, None)
                asyncio.get_running_loop().create_task(_close_quietly(ws))   # 繋ぎ直せば、最初の 1 通で一覧から届く
            except Exception:
                self.subscribers.pop(ws, None)
        if device_msg is not None:
            raw2 = json.dumps(device_msg, ensure_ascii=False)
            for dev in list(self.devices.values()):
                if did and not self._can_see(dev.who, did):
                    continue
                try:
                    await asyncio.wait_for(dev.ws.send(raw2), BROADCAST_SEND_SECS)
                except Exception:
                    pass

    async def _events(self, ws) -> None:
        """スマホ・ウェブ版の知らせの線。最初に走っている会話と機械の一覧、その後は変わるたびに 1 通。"""
        who = getattr(ws, "bw_who", None)
        self.subscribers[ws] = who
        try:
            await ws.send(json.dumps({"type": "hello", "relay": VERSION, "devices": self.device_list(who),
                                      "active": self.conversations(active=True, limit=100, who=who)},
                                     ensure_ascii=False))
            async for _ in ws:                 # 送られてくる物は無い（ping は websockets が返す）
                pass
        except Exception:
            pass
        finally:
            self.subscribers.pop(ws, None)

    # ── アカウント（/auth）────────────────────────────────────────
    def _peer_ip(self, connection) -> str:
        """本当の相手の住所（手前の口を通った線は、手前の口が覚えた住所）。"""
        try:
            addr = connection.remote_address
        except Exception:
            return ""
        if not addr:
            return ""
        if not self.tls_on:                # 手前の口を使っていない＝線は相手から直に来ている
            return str(addr[0])
        # ★手前の口を通る線は全部 127.0.0.1 から来る。覚えが無いときに addr[0] を返すと LAN の相手が「この機械の中」に見える
        #   （2026-11-13: 覚えの鍵がこちらの口の番号だけで、平文と TLS の 2 つの内側の口への線が同じ番号を取ると上書きされ、
        #   先に閉じた方が覚えを消していた）。鍵は（こちらの口, 内側の口）。無ければ信用しない。
        try:
            inner = connection.local_address[1]
        except Exception:
            inner = 0
        return self.front_peers.get((addr[1], inner), "")

    def _secure(self, connection) -> bool:
        """線が TLS か、本当にこの機械の中からか（パスワードと認証情報はこのどちらかでしか受けない）。"""
        try:
            if connection.transport.get_extra_info("ssl_object") is not None:
                return True
        except AttributeError:
            pass
        return self._peer_ip(connection) in _LOOPBACK

    def _identify(self, request, q) -> tuple[dict | None, bool]:
        """(相手, 認証情報が付いていたか)。付いていて無効なら (None, True)。"""
        raw = (q.get("token") or [""])[0] or request.headers.get("X-Blackwall-Token", "")
        if not raw:
            return None, False
        if self.token is None and self.accounts is None:
            # ★アカウントも共有の合言葉も使わない設定（off）。付いてきた認証情報は見ない（2026-11-13: 無効と答えると、客は
            #   手元の認証情報を捨てる＝中継を off に戻しただけで全部の機械がサインアウトする）。
            return None, False
        if self.token is not None and hmac.compare_digest(raw.encode("utf-8"), self.token.encode("utf-8")):
            return {"aid": "*", "id": "", "name": "shared"}, True
        return (self.accounts.check(raw) if self.accounts is not None else None), True

    def _limited(self, key: str, limit: int = FAIL_MAX, window: float = FAIL_WINDOW) -> bool:
        now = time.time()
        hits = [t for t in self.fails.get(key, []) if now - t < window]
        if hits:
            self.fails[key] = hits
        else:
            self.fails.pop(key, None)
        return len(hits) >= limit

    def _hit(self, *keys: str) -> None:
        if len(self.fails) > 10000:          # 数えの表を無限に太らせない。★期限の切れた物から捨てる（全部消すと全員の制限が外れる）
            now = time.time()
            for k in [k for k, v in self.fails.items() if not v or now - v[-1] >= FAIL_WINDOW]:
                self.fails.pop(k, None)
            if len(self.fails) > 10000:
                self.fails.clear()
        for k in keys:
            self.fails.setdefault(k, []).append(time.time())

    def _unhit(self, *keys: str) -> None:
        """先に数えた 1 回を戻す（照合が通ったとき）。"""
        for k in keys:
            hits = self.fails.get(k)
            if hits:
                hits.pop()
                if not hits:
                    self.fails.pop(k, None)

    async def _hashed(self, fn, *args):
        """重い計算（パスワードの照合）は別の糸で、同時に 2 本まで（中継の線を止めない・メモリを食い潰さない）。"""
        async with self._hash_gate:
            return await asyncio.to_thread(fn, *args)

    def _close_token_lines(self, tid: str, keep=None) -> None:
        """その認証情報で開いている線を閉じる。keep（いま答えを返す線）は閉じない（相手が答えを受け取ってから閉じる）。"""
        for ws in list(self.live.pop(tid, ())):
            if ws is not keep:
                asyncio.create_task(self._close_quietly(ws))

    @staticmethod
    async def _close_quietly(ws) -> None:
        try:
            await ws.close(4401, "signed out")
        except Exception:
            pass

    def _account_view(self, aid: str) -> dict:
        return {"id": aid, "email": self.accounts.accounts[aid]["email"]}

    def _pairing_sweep(self) -> None:
        now = time.time()
        for code in [c for c, p in self.pairing.items() if now > p["exp"]]:
            self.pairing.pop(code, None)

    async def _auth_rpc(self, ws) -> None:
        """サインインの線。1 行の JSON を受けて 1 行で答える。"""
        from websockets.exceptions import ConnectionClosed
        try:
            for _ in range(AUTH_MSGS_PER_LINE):
                raw = await asyncio.wait_for(ws.recv(), 60)
                if not isinstance(raw, str) or len(raw) > AUTH_MSG_MAX:
                    await ws.send(json.dumps({"ok": False, "error": "bad_request"}))
                    continue
                try:
                    msg = json.loads(raw)
                except ValueError:
                    msg = None
                reply = await self._auth_op(ws, msg if isinstance(msg, dict) else {})
                await ws.send(json.dumps(reply, ensure_ascii=False))
        except (asyncio.TimeoutError, ConnectionClosed):
            pass
        except Exception as e:  # noqa: BLE001 — 線ごと落とさず、理由を残す（値は出さない）
            _log(f"/auth の処理で失敗: {type(e).__name__}")

    async def _auth_op(self, ws, msg: dict) -> dict:
        op = str(msg.get("op") or "")
        who = getattr(ws, "bw_who", None)
        ip = self._peer_ip(ws)
        if op == "status":
            return {"ok": True, "mode": self.auth_mode, "signup": self._signup_state(), "relay": VERSION,
                    "account": self._account_view(who["aid"]) if who and who.get("aid") in self.accounts.accounts else None}
        if op == "register":
            return await self._op_register(msg, ip)
        if op == "login":
            return await self._op_login(msg, ip)
        if op == "pair_start":
            return self._op_pair_start(msg, ip)
        if op == "pair_poll":
            return self._op_pair_poll(msg)
        if op in ("admin_reset", "admin_delete"):
            if ip not in _LOOPBACK:
                return {"ok": False, "error": "forbidden"}
            return await self._op_admin(op, msg)
        # ここから先はサインインした線だけ
        if not who or who.get("aid") not in self.accounts.accounts:
            return {"ok": False, "error": "unauthorized"}
        aid = who["aid"]
        if who.get("scope") == MACHINE_SCOPE and op != "logout":
            return {"ok": False, "error": "forbidden", "message": "この機械の認証情報では操作できません"}
        if op == "sessions":
            items = [{"id": r["id"], "name": r.get("name") or "", "kind": r.get("kind") or "",
                      "scope": r.get("scope") or "",
                      "created": r.get("created"), "last_used": r.get("last_used"), "current": r["id"] == who["id"],
                      "device": r.get("device") or "", "online": r.get("device") in self.devices}
                     for r in self.accounts.of_account(aid)]
            items.sort(key=lambda r: r.get("last_used") or 0, reverse=True)
            return {"ok": True, "items": items}
        if op == "machines":
            return {"ok": True, "items": self.device_list(who)}
        if op in ("logout", "revoke"):
            tid = who["id"] if op == "logout" else str(msg.get("id") or "")
            if not self.accounts.drop_token(aid, tid):
                return {"ok": False, "error": "not_found"}
            # ★自分でサインアウトした機械は誰の物でもなくなる（別のアカウントでサインインし直せる）。
            #   別の端末から取り消した機械は持ち主のまま（盗まれた機械が別のアカウントで名乗れないように）。
            info = self.seen.get(who.get("device") or "") if op == "logout" else None
            if info is not None and info.get("owner") == aid:
                info.pop("owner", None)
                info["was_owner"] = aid          # 目録と写しはこのアカウントの物（別のアカウントが繋いだら避ける）
                self._save_seen()
            self._close_token_lines(tid, keep=ws)
            return {"ok": True}
        if op == "password":
            return await self._op_password(msg, aid, who["id"], ip)
        if op == "pair_approve":
            return self._op_pair_approve(msg, aid, ip)
        if op == "forget_machine":
            return await self._op_forget_machine(msg, aid, ws)
        if op == "conv_delete":
            return await self._op_conv_delete(msg, who)
        return {"ok": False, "error": "bad_request"}

    async def _op_conv_delete(self, msg: dict, who: dict) -> dict:
        """会話を中継から消す（1.8・オーナー 11-11「消さないで残しておいてアプリから消せるようにしておく」）。
        中継の写しと一覧から消し、同じ会話が機械から来ても戻さない。★元の機械の記録には触らない（その機械のアプリで消す）。"""
        did, sid = str(msg.get("device") or ""), str(msg.get("sid") or "")
        if not (_ID_RE.fullmatch(did) and _safe_name(sid)):
            return {"ok": False, "error": "bad_request"}
        if not self._can_see(who, did):
            return {"ok": False, "error": "not_found"}
        had = self.journal.delete(did, sid)
        had = (self.index.get(did) or {}).pop(sid, None) is not None or had
        self._save_index_soon()
        (self.sync_want.get(did) or {}).pop(sid, None)
        if had:
            await self._broadcast({"type": "conv", "item": {"sid": sid, "device": did, "removed": True}},
                                  {"op": "peer_conv", "item": {"sid": sid, "device": did, "removed": True}}, did=did)
        _log(f"会話を消した: {did}/{sid}（{'あり' if had else '無し'}）")
        return {"ok": True, "removed": had}

    async def _op_register(self, msg: dict, ip: str) -> dict:
        closed = {"ok": False, "error": "signup_closed", "message": "新しい登録は閉じています"}
        if self._signup_state() != "open":
            return closed
        email, password = normalize_email(msg.get("email")), str(msg.get("password") or "")
        if len(email) > 254 or not _EMAIL_RE.fullmatch(email):
            return {"ok": False, "error": "bad_email", "message": "メールアドレスの形が違います"}
        if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            return {"ok": False, "error": "weak_password", "message": f"パスワードは {PASSWORD_MIN} 文字以上にしてください"}
        if self._limited(f"signup:{ip}", SIGNUP_MAX, SIGNUP_WINDOW):
            return {"ok": False, "error": "too_many", "message": "登録の回数が多すぎます。しばらく待ってください"}
        if self.accounts.by_email(email) is not None:
            return {"ok": False, "error": "email_taken", "message": "このメールアドレスは登録済みです"}
        self._hit(f"signup:{ip}")
        record = await self._hashed(Accounts._password_record, password)
        # 計算の間に別の登録が通っていることがある
        if self._signup_state() != "open":
            return closed
        if self.accounts.by_email(email) is not None:
            return {"ok": False, "error": "email_taken", "message": "このメールアドレスは登録済みです"}
        aid = self.accounts.add(email, record)
        raw, _rec = self.accounts.issue(aid, msg.get("device"))
        _log(f"アカウントを作った: {aid}（{len(self.accounts.accounts)} 件）")
        return {"ok": True, "token": raw, "account": self._account_view(aid)}

    async def _op_login(self, msg: dict, ip: str) -> dict:
        email, password = normalize_email(msg.get("email")), str(msg.get("password") or "")
        keys = (f"ip:{ip}", f"mail:{email}")
        if any(self._limited(k) for k in keys):
            return {"ok": False, "error": "too_many", "message": "失敗が続いたので止めました。5 分ほど待ってください"}
        # ★照合の前に数える（2026-11-13: 確かめ → 重い照合を待つ → 数える、の順だったので、並べて送った分は全部が確かめを先に通り、
        #   「5 分に 10 回」が効かなかった）。通ったら戻す。
        self._hit(*keys)
        aid = await self._hashed(self.accounts.verify, email, password[:PASSWORD_MAX]) if email else None
        if aid is None:
            return {"ok": False, "error": "bad_login", "message": "メールアドレスかパスワードが違います"}
        self._unhit(*keys)
        raw, _rec = self.accounts.issue(aid, msg.get("device"))
        _log(f"サインイン: {aid}")
        return {"ok": True, "token": raw, "account": self._account_view(aid)}

    async def _op_password(self, msg: dict, aid: str, keep_tid: str, ip: str) -> dict:
        old, new = str(msg.get("old") or ""), str(msg.get("new") or "")
        if self._limited(f"ip:{ip}"):
            return {"ok": False, "error": "too_many", "message": "失敗が続いたので止めました。5 分ほど待ってください"}
        if not PASSWORD_MIN <= len(new) <= PASSWORD_MAX:
            return {"ok": False, "error": "weak_password", "message": f"パスワードは {PASSWORD_MIN} 文字以上にしてください"}
        self._hit(f"ip:{ip}")                # 照合の前に数える（_op_login と同じ理由）
        if not await self._hashed(self.accounts.password_matches, aid, old[:PASSWORD_MAX]):
            return {"ok": False, "error": "bad_login", "message": "いまのパスワードが違います"}
        self._unhit(f"ip:{ip}")
        self.accounts.set_record(aid, await self._hashed(Accounts._password_record, new))
        # ★パスワードを変えたら、ほかの端末はサインインし直す（漏れて変えたのに、漏れた先が残らないように）
        for rec in self.accounts.of_account(aid):
            if rec["id"] != keep_tid:
                self.accounts.drop_token(aid, rec["id"])
                self._close_token_lines(rec["id"])
        return {"ok": True}

    def _op_pair_start(self, msg: dict, ip: str) -> dict:
        self._pairing_sweep()
        if len(self.pairing) >= PAIR_MAX or self._limited(f"pair:{ip}", 30, 600.0):
            return {"ok": False, "error": "too_many", "message": "待っている機械が多すぎます。しばらく待ってください"}
        self._hit(f"pair:{ip}")
        code = secrets.token_urlsafe(32)
        user = "".join(secrets.choice(_PAIR_ALPHABET) for _ in range(8))
        d = msg.get("device") if isinstance(msg.get("device"), dict) else {}
        self.pairing[code] = {"user": user, "device": d, "exp": time.time() + PAIR_KEEP_SECS, "aid": None}
        return {"ok": True, "code": code, "user_code": f"{user[:4]}-{user[4:]}", "expires_in": int(PAIR_KEEP_SECS)}

    def _op_pair_poll(self, msg: dict) -> dict:
        self._pairing_sweep()
        code = str(msg.get("code") or "")
        p = self.pairing.get(code)
        if p is None:
            return {"ok": False, "error": "expired", "message": "コードの期限が切れました"}
        if p["aid"] is None:
            return {"ok": True, "pending": True}
        self.pairing.pop(code, None)
        if p["aid"] not in self.accounts.accounts:       # 承認した後にアカウントが消えた（持ち主のいない認証情報を作らない）
            return {"ok": False, "error": "expired", "message": "コードの期限が切れました"}
        raw, _rec = self.accounts.issue(p["aid"], p["device"], p.get("scope") or "")
        return {"ok": True, "token": raw, "account": self._account_view(p["aid"])}

    def _op_pair_approve(self, msg: dict, aid: str, ip: str) -> dict:
        self._pairing_sweep()
        # ★当てずっぽうで他人の機械を自分のアカウントに入れられないよう、外れの回数を数える
        if self._limited(f"pairfail:{ip}"):
            return {"ok": False, "error": "too_many", "message": "失敗が続いたので止めました。5 分ほど待ってください"}
        user = re.sub(r"[^A-Za-z0-9]", "", str(msg.get("user_code") or "")).upper()
        for p in self.pairing.values():
            if p["aid"] is None and hmac.compare_digest(p["user"].encode(), user.encode()):
                p["aid"] = aid
                p["scope"] = MACHINE_SCOPE if msg.get("scope") == MACHINE_SCOPE else ""
                return {"ok": True, "device": {"name": str(p["device"].get("name") or "")[:80]}}
        self._hit(f"pairfail:{ip}")
        return {"ok": False, "error": "not_found", "message": "コードが見つかりません。もう一度確かめてください"}

    async def _op_forget_machine(self, msg: dict, aid: str, ws) -> dict:
        """機械を一覧から外す。自分の機械か、繋がっていない誰の物でもない機械だけ（Windows の VM の古い登録のような行を消す）。"""
        did = str(msg.get("id") or "")
        info = self.seen.get(did)
        if info is None or not self._can_see({"aid": aid}, did):
            return {"ok": False, "error": "not_found"}
        dev = self.devices.get(did)
        if info.get("owner") is None and dev is not None:
            return {"ok": False, "error": "online", "message": "繋がっている間は外せません"}
        for rec in self.accounts.of_account(aid):
            if rec.get("device") == did:
                self.accounts.drop_token(aid, rec["id"])
                self._close_token_lines(rec["id"], keep=ws)
        self.seen.pop(did, None)
        self._set_aside(did, "一覧から外した")   # 写しと、引く列も残さない（同じ名札の別の機械に見せない）
        self._save_seen()
        self._save_index_soon()
        if dev is not None:
            await self._close_quietly(dev.ws)
        return {"ok": True}

    async def _op_admin(self, op: str, msg: dict) -> dict:
        hit = self.accounts.by_email(normalize_email(msg.get("email")))
        if hit is None:
            return {"ok": False, "error": "not_found", "message": "そのメールアドレスのアカウントはありません"}
        aid = hit[0]
        if op == "admin_delete":
            for tid in self.accounts.delete_account(aid):
                self._close_token_lines(tid)
            for did, info in self.seen.items():
                if info.get("owner") == aid:
                    info.pop("owner", None)      # 機械は残す（誰の物でもなくなる＝別のアカウントでサインインできる）
                    info["was_owner"] = aid      # 目録と写しは消したアカウントの物（別のアカウントが繋いだら避ける）
            self._save_seen()
            return {"ok": True}
        password = str(msg.get("password") or "")
        if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
            return {"ok": False, "error": "weak_password", "message": f"パスワードは {PASSWORD_MIN} 文字以上にしてください"}
        self.accounts.set_record(aid, await self._hashed(Accounts._password_record, password))
        for rec in self.accounts.of_account(aid):
            self.accounts.drop_token(aid, rec["id"])
            self._close_token_lines(rec["id"])
        return {"ok": True}

    # ── HTTP ──────────────────────────────────────────────────────
    def _web(self, connection, path: str):
        """ウェブ版の画面のファイル。当たらなければ None（API の扱いへ回す）。"""
        name = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        if not _WEB_NAME.fullmatch(name) or ".." in name or "/." in "/" + name:
            return None
        ctype = _WEB_TYPES.get(Path(name).suffix.lower())
        if ctype is None:
            return None
        try:
            f = (WEB_DIR / name).resolve()
            f.relative_to(WEB_DIR.resolve())
            if not f.is_file():
                return None
            body = f.read_bytes()
        except (OSError, ValueError):
            return None
        r = connection.respond(HTTPStatus.OK, "")
        del r.headers["Content-Type"]
        del r.headers["Content-Length"]
        r.headers["Content-Type"] = ctype
        r.headers["Content-Length"] = str(len(body))
        # 画面は出荷のたびに変わる。古い写しで動かないよう、毎回確かめさせる（フォントは長く持つ）。
        r.headers["Cache-Control"] = "public, max-age=604800" if ctype.startswith("font/") else "no-cache"
        r.body = body
        return r

    async def _metrics(self, connection, since: str):
        try:
            s = max(0.0, float(since))
        except ValueError:
            s = 0.0
        url = METRICS_URL + ("&" if "?" in METRICS_URL else "?") + urlencode({"since": f"{s:.1f}"})

        def get():
            import urllib.request
            with urllib.request.urlopen(url, timeout=5) as r:
                return r.read().decode("utf-8", "replace")
        try:
            body = await asyncio.to_thread(get)
        except Exception as e:
            return connection.respond(HTTPStatus.BAD_GATEWAY, json.dumps(
                {"error": f"bw-metrics に届きません（{type(e).__name__}）"}, ensure_ascii=False))
        return connection.respond(HTTPStatus.OK, body)

    @staticmethod
    def _foreign_origin(request) -> bool:
        """ブラウザのほかのサイトから開いた線か。★WebSocket に同一生成元の制限は無いので、確かめないと開いたページが機械を操作できる。
        ブラウザ以外の客（アプリ・端末版・スマホ）は Origin を付けない。"""
        origin = request.headers.get("Origin", "")
        return bool(origin) and urlsplit(origin).netloc.lower() != request.headers.get("Host", "").lower()

    async def process_request(self, connection, request):
        if self._foreign_origin(request):
            return connection.respond(HTTPStatus.FORBIDDEN, "cross-origin request refused\n")
        u = urlsplit(request.path)
        path, q = u.path, parse_qs(u.query)
        secure = self._secure(connection)
        if not path.startswith(("/d/", "/_device/")) and path not in _API_PATHS:
            if self.tls_on and not secure and path in ("/", "/index.html"):
                # ★画面は TLS で出す（サインインの画面からパスワードを平文で送らせない）
                r = connection.respond(HTTPStatus.FOUND, "")
                r.headers["Location"] = f"https://{request.headers.get('Host', '')}{path}"
                return r
            page = self._web(connection, path)
            if page is not None:
                return page
        if path == "/health":
            return connection.respond(HTTPStatus.OK, json.dumps(
                {"ok": True, "relay": True, "version": f"relay {VERSION}",
                 "devices": len(self.devices), "auth": self.token is not None or self.auth_mode == "required",
                 "auth_mode": self.auth_mode, "signup": self._signup_state(), "tls": self.tls_on,
                 "web": (WEB_DIR / "index.html").is_file()}))
        who, given = self._identify(request, q)
        connection.bw_who = who
        if given and not secure and self.tls_on:
            return connection.respond(HTTPStatus.FORBIDDEN, "use TLS\n")   # 平文で届いた認証情報は使わない
        if given and who is None:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "token invalid\n")
        # ★接続だけの認証情報（作業場の 作業用のコンテナ など・中でだれでもコマンドを走らせられる機械）は、名乗る線とサインアウトだけ。
        #   これが漏れても、ほかの機械の一覧も操作の線も開けない。
        if who is not None and who.get("scope") == MACHINE_SCOPE and path not in ("/_device/control", "/_device/data", "/auth"):
            return connection.respond(HTTPStatus.FORBIDDEN, "this credential can only connect the machine\n")
        if path == "/auth":
            if self.auth_mode == "off" or self.accounts is None:
                return connection.respond(HTTPStatus.NOT_FOUND, "accounts are off\n")
            if not secure:
                return connection.respond(HTTPStatus.FORBIDDEN, "use TLS\n")
            return None                        # WebSocket（handler の _auth_rpc）
        if path in ("/_device/control", "/_device/data"):
            if self.device_key is not None:
                got = (q.get("key") or [""])[0]
                if not secrets.compare_digest(got.encode("utf-8"), self.device_key.encode("utf-8")):
                    return connection.respond(HTTPStatus.UNAUTHORIZED, "device key required\n")
            did = (q.get("id") or [""])[0]
            if not _ID_RE.fullmatch(did):
                return connection.respond(HTTPStatus.BAD_REQUEST, "bad device id\n")
            if self.auth_mode == "required" and who is None:
                return connection.respond(HTTPStatus.UNAUTHORIZED, "sign in required\n")
            # ★名札は秘密ではない。持ち主のいる機械の名札で別のアカウントが名乗ると、本物を押し出して乗っ取れてしまう。
            owner = (self.seen.get(did) or {}).get("owner")
            if owner and (who is None or who.get("aid") not in (owner, "*")):
                return connection.respond(HTTPStatus.FORBIDDEN, "this machine belongs to another account\n")
            return None
        if (self.token is not None or self.auth_mode == "required") and who is None:
            return connection.respond(HTTPStatus.UNAUTHORIZED, "token required\n")
        if path == "/devices":
            return connection.respond(HTTPStatus.OK, json.dumps(self.device_list(who), ensure_ascii=False))
        if path == "/conversations":
            try:
                lim = int((q.get("limit") or ["200"])[0])
            except ValueError:
                lim = 200
            rows = self.conversations(limit=min(max(lim, 1), 1000), q=(q.get("q") or [""])[0],
                                      active=(q.get("active") or ["0"])[0] in ("1", "true"),
                                      device=(q.get("device") or [""])[0], who=who)
            return connection.respond(HTTPStatus.OK, json.dumps(rows, ensure_ascii=False))
        if path == "/events":
            return None                        # WebSocket（handler の _events）
        if path == "/server/metrics":
            # ★推論機の機械の GPU（使用率・温度・VRAM）を Android とウェブ版に出す（2026-10-18 オーナー「各アプリの UI に
            #   2080super の使用率を表示する必要がありますね」）。bw-metrics の答えをそのまま返す（読むだけ・合言葉の後）。
            return await self._metrics(connection, (q.get("since") or ["0"])[0])
        m = re.fullmatch(r"/d/([A-Za-z0-9_.\-]{1,64})(/.*)?", path)
        if not m:
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        did, rest = m.group(1), m.group(2) or "/"
        if not self._can_see(who, did):
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")    # 別のアカウントの機械は「無い」と答える
        dev = self.devices.get(did)
        ev = re.fullmatch(r"/sessions/([A-Za-z0-9_.\-]{1,80})/events", rest)
        if ev and _safe_name(ev.group(1)):
            # ★中身は中継の写しから出す（1.8）。機械が繋いでいれば先に続きを引いてから（最新にする）、寝ていれば写しのまま。
            sid = ev.group(1)
            if self.journal.is_gone(did, sid):
                return connection.respond(HTTPStatus.NOT_FOUND, "no such session\n")
            if dev is not None:
                try:
                    await self.sync_one(did, sid, (self.index.get(did) or {}).get(sid, {}).get("updated"), timeout=8.0)
                except Exception:
                    pass                         # 引けなくても、持っている写しを出す
            try:
                since = int((q.get("since_seq") or ["0"])[0]); limit = int((q.get("limit") or ["0"])[0])
            except ValueError:
                since, limit = 0, 0
            body = self.journal.read(did, sid, since_seq=since, limit=limit)
            if body is not None:
                r = connection.respond(HTTPStatus.OK, body)
                r.headers["X-Blackwall-Copy"] = "relay" + ("" if dev is not None else "; offline")
                return r
        if dev is None:
            return connection.respond(HTTPStatus.SERVICE_UNAVAILABLE, "device offline\n")
        # ★中継の合言葉（token）は機械へ渡さない（中継のための物で、機械の側では要らない・1.1）。
        fwd = urlencode({k: v[0] for k, v in q.items() if v and k != "token"})
        if rest == "/ws":
            # ★1.0 は先に 101 を返してから機械に線を開かせていた。スマホは「つながった」と表示し、
            #   その間に送った依頼は宙に浮き、開けなければ 4504 で切られた（敵対レビュー 2026-10-08）。
            #   1.1 は機械の側の線が開いてから受け入れる。開けなければ握手の段で 504 を返す。
            try:
                data, ended = await self._open_device(dev, fwd)
            except Exception as e:
                return connection.respond(HTTPStatus.GATEWAY_TIMEOUT, f"device could not open: {e}\n"[:200])
            # ★預ける鍵は接続の物そのもの（1.4・2026-10-25 敵対レビュー）。旧は id(connection) で、握手が途中で切れた接続の
            #   預かりが 30 秒残る間に、解放された物の id が新しい接続に使い回されると、別の機械の線をその接続につないでいた。
            #   物を鍵にすれば預かっている間は解放されない。取り出すときは機械の名前も突き合わせる。
            self.preopened[connection] = (did, data, ended)
            asyncio.get_running_loop().call_later(30, self._expire_preopen, connection, ended)
            return None
        if rest.startswith("/web"):
            return connection.respond(HTTPStatus.NOT_FOUND, "not served through the relay\n")
        try:
            status, body = await dev.http(rest + (f"?{fwd}" if fwd else ""))
        except asyncio.TimeoutError:
            return connection.respond(HTTPStatus.GATEWAY_TIMEOUT, "device did not answer\n")
        except Exception as e:
            return connection.respond(HTTPStatus.BAD_GATEWAY, f"device error: {e}\n")
        try:
            st = HTTPStatus(status)
        except ValueError:
            st = HTTPStatus.BAD_GATEWAY
        return connection.respond(st, body)

    # ── WebSocket ─────────────────────────────────────────────────
    async def handler(self, connection) -> None:
        who = getattr(connection, "bw_who", None)
        tid = who.get("id") if who else ""
        if tid:
            self.live.setdefault(tid, set()).add(connection)   # サインアウトされたら閉じる
        try:
            await self._route(connection)
        finally:
            if tid and tid in self.live:
                self.live[tid].discard(connection)
                if not self.live[tid]:
                    self.live.pop(tid, None)

    async def _route(self, connection) -> None:
        u = urlsplit(connection.request.path)
        q = parse_qs(u.query)
        if u.path == "/_device/control":
            await self._device_control(connection, (q.get("id") or [""])[0])
        elif u.path == "/_device/data":
            await self._device_data(connection, (q.get("cid") or [""])[0])
        elif u.path == "/events":
            await self._events(connection)
        elif u.path == "/auth":
            await self._auth_rpc(connection)
        else:
            m = re.fullmatch(r"/d/([A-Za-z0-9_.\-]{1,64})/ws", u.path)
            pre = self.preopened.pop(connection, None)
            if pre is not None and not (m and pre[0] == m.group(1)):
                pre[2].set()                     # 別の機械の線は使わない（畳む）
                pre = None
            if m and pre is not None:
                _did, data, ended = pre
                try:
                    await splice(connection, data)
                finally:
                    ended.set()
            elif m:
                q.pop("token", None)
                query = urlencode({k: v[0] for k, v in q.items() if v})
                await self._phone_ws(connection, m.group(1), query)
            else:
                await connection.close(4404, "not found")

    async def _device_control(self, ws, did: str) -> None:
        try:
            first = json.loads(await asyncio.wait_for(ws.recv(), 15))
        except Exception:
            await ws.close(4400, "hello expected")
            return
        info = first.get("info") if isinstance(first, dict) and first.get("op") == "hello" else None
        if not isinstance(info, dict):
            await ws.close(4400, "hello expected")
            return
        info = {k: v for k, v in info.items() if k in ("name", "os", "kind", "version", "workspace", "user")}
        old = self.devices.get(did)
        if old is not None:
            try:
                await old.ws.close(4000, "replaced by a new connection")
            except Exception:
                pass
        who = getattr(ws, "bw_who", None)
        dev = Device(did, info, ws, who)
        self.devices[did] = dev
        # ★サインインした線で繋いだ機械は、そのアカウントの物になる。認証情報にもどの機械の物かを書く（一覧で外せるように）。
        owner = who.get("aid") if who and who.get("aid") != "*" else None
        self._remember(did, info, owner=owner)
        if owner and who.get("device") != did:
            who["device"] = did
            self.accounts.save()
        _log(f"機械が繋いだ: {info.get('name')} [{did}] {info.get('kind')} {info.get('version')}"
             f"（{len(self.devices)} 台）")
        try:
            await dev.send({"op": "peers", "items": self.conversations(limit=300, who=who)})
        except Exception:
            pass
        await self._broadcast({"type": "device", "device": {**info, "id": did, "online": True}}, did=did)
        try:
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                op = msg.get("op")
                if op == "http_reply":
                    fut = dev.pending.get(int(msg.get("rid") or 0))
                    if fut and not fut.done():
                        fut.set_result(msg)
                elif op == "open_failed":
                    fut = self.waiters.get(str(msg.get("cid") or ""))
                    if fut and not fut.done():
                        fut.set_exception(RuntimeError(str(msg.get("error") or "open failed")))
                elif op == "index":
                    for it in self.apply_index(did, msg):
                        await self._broadcast({"type": "conv", "item": it}, {"op": "peer_conv", "item": it}, did=did)
                    self.want_sync(did, msg.get("items"))     # 中身の写しを引く（1.8）
                elif op == "info" and isinstance(msg.get("info"), dict):
                    dev.info.update({k: v for k, v in msg["info"].items()
                                     if k in ("name", "os", "kind", "version", "workspace", "user")})
                    self._remember(did, dev.info)
        except Exception as e:
            _log(f"機械 {did}: {type(e).__name__}: {e}")
        finally:
            if self.devices.get(did) is dev:
                self.devices.pop(did, None)
                if did in self.seen:             # 一覧から外された機械（forget_machine）を切断の記録で戻さない
                    self._remember(did, dev.info)
                # ★切れた機械で走っていた会話は、もう誰も「終わった」と言わない＝走っているまま残さない
                gone = []
                for it in (self.index.get(did) or {}).values():
                    if it.get("status") in ("running", "asking"):
                        it["status"] = "idle"
                        it.pop("ask", None)
                        gone.append(self._with_device(did, it))
                self._save_index_soon()
                try:
                    await self._broadcast({"type": "device", "device": {**dev.info, "id": did, "online": False}}, did=did)
                    for it in gone:
                        await self._broadcast({"type": "conv", "item": it}, {"op": "peer_conv", "item": it}, did=did)
                except Exception:
                    pass
            for fut in dev.pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("device went away"))
            _log(f"機械が切れた: {dev.info.get('name')} [{did}]（{len(self.devices)} 台）")

    async def _device_data(self, ws, cid: str) -> None:
        fut = self.waiters.get(cid)
        if fut is None or fut.done():
            await ws.close(4404, "no such pending connection")
            return
        ended = asyncio.Event()
        fut.set_result((ws, ended))
        await ended.wait()                  # 中継が両方を繋いでいる間は、この接続を閉じない

    async def _open_device(self, dev: Device, query: str):
        """機械に /ws の線を 1 本開かせ、開いたら (線, 終わりの合図) を返す。20 秒で諦める。"""
        cid = secrets.token_hex(8)
        fut = asyncio.get_running_loop().create_future()
        self.waiters[cid] = fut
        try:
            await dev.send({"op": "open", "cid": cid, "path": "/ws" + (f"?{query}" if query else "")})
            return await asyncio.wait_for(fut, 20)
        finally:
            self.waiters.pop(cid, None)

    def _expire_preopen(self, key, ended: asyncio.Event) -> None:
        """握手が途中で終わって受け取り手の来なかった線を畳む（機械の側の線を残さない）。"""
        pre = self.preopened.get(key)
        if pre is not None and pre[2] is ended:
            self.preopened.pop(key, None)
            ended.set()

    async def _phone_ws(self, phone, did: str, query: str) -> None:
        """預かった線が無いとき（握手の前に機械が消えた等）の予備。"""
        dev = self.devices.get(did)
        if dev is None:
            await phone.close(4503, "device offline")
            return
        try:
            data, ended = await self._open_device(dev, query)
        except Exception as e:
            await phone.close(4504, f"device could not open: {e}"[:120])
            return
        try:
            await splice(phone, data)
        finally:
            ended.set()


async def splice(a, b) -> None:
    """2 本の WebSocket をつなぐ。片方が閉じたら、同じ閉じ方でもう片方も閉じる（close コードを運ぶ:
    4001＝serve が終わった・4000＝別の接続に引き継いだ。画面はこれで「切れた」か「終わった」かを分ける）。"""
    async def pump(src, dst) -> None:
        try:
            async for msg in src:
                await dst.send(msg)
        except Exception:
            pass

    t1 = asyncio.create_task(pump(a, b))
    t2 = asyncio.create_task(pump(b, a))
    done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
    first = a if t1 in done else b
    other = b if first is a else a
    code = getattr(first, "close_code", None) or 1000
    reason = getattr(first, "close_reason", None) or ""
    try:
        await other.close(code if code not in (1005, 1006) else 1001, reason)
    except Exception:
        pass
    for t in pending:
        t.cancel()


async def _close_quietly(ws) -> None:
    try:
        await asyncio.wait_for(ws.close(), 5)
    except Exception:
        pass


def _front(relay: Relay, plain_port: int, tls_port: int):
    """1 つの口で平文と TLS の両方を受ける手前の口。最初の 1 バイト（TLS の握手は 0x16）で内側の 2 つの口へ振り分ける。
    ★内側の口は websockets のサーバそのまま（ここはバイトを運ぶだけ）。本当の相手の住所は relay.front_peers に覚える。"""
    async def pipe(src, dst) -> None:
        try:
            while True:
                data = await src.read(65536)
                if not data:
                    break
                dst.write(data)
                await dst.drain()
        except (OSError, ConnectionError):
            pass
        finally:
            dst.close()                    # 片方が閉じたら両方閉じる（書きかけの分は送ってから閉じる）

    async def on_conn(r, w) -> None:
        peer = w.get_extra_info("peername")
        try:
            first = await asyncio.wait_for(r.read(1), FRONT_FIRST_SECS)
        except (asyncio.TimeoutError, OSError, ConnectionError):
            first = b""
        if not first:
            w.close()
            return
        inner = tls_port if first == b"\x16" else plain_port
        try:
            r2, w2 = await asyncio.open_connection("127.0.0.1", inner)
        except OSError as e:
            _log(f"内側の口に繋げない: {e!r}")
            w.close()
            return
        local = (w2.get_extra_info("sockname")[1], inner)
        relay.front_peers[local] = str(peer[0]) if peer else ""
        w2.write(first)
        try:
            await asyncio.gather(pipe(r, w2), pipe(r2, w))
        finally:
            relay.front_peers.pop(local, None)
            w.close()
            w2.close()
    return on_conn


def _tls_context(cert: Path, key: Path) -> ssl.SSLContext | None:
    """証明書と鍵があれば TLS を受ける。無ければ None（平文だけ・アカウントの線は手元からしか使えない）。"""
    if not (cert.is_file() and key.is_file()):
        return None
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


async def run(relay: Relay, host: str, port: int, tls: ssl.SSLContext | None = None,
              started: asyncio.Event | None = None) -> None:
    from websockets.asyncio.server import serve
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, ValueError, RuntimeError):   # 主の糸でないとき（試験）は合図を受けない
            pass
    relay.stop = stop
    opts = dict(process_request=relay.process_request, open_timeout=OPEN_TIMEOUT, max_size=32 * 1024 * 1024, ping_interval=20, ping_timeout=30)

    def said() -> None:
        _log(f"relay {VERSION} listening {host}:{port}  auth={relay.auth_mode} signup={relay.signup}"
             f"  tls={'on' if relay.tls_on else 'OFF'}  shared-token={'on' if relay.token else 'OFF'}"
             f"  device-key={'on' if relay.device_key else 'OFF'}")
        if started is not None:
            started.set()

    if tls is None:
        try:
            async with serve(relay.handler, host, port, **opts):
                said()
                await stop.wait()
        finally:
            relay.flush()
        return
    relay.tls_on = True
    async with serve(relay.handler, "127.0.0.1", 0, **opts) as plain, \
            serve(relay.handler, "127.0.0.1", 0, ssl=tls, ssl_handshake_timeout=10, **opts) as secure:
        front = await asyncio.start_server(_front(relay, plain.sockets[0].getsockname()[1],
                                                  secure.sockets[0].getsockname()[1]), host, port, backlog=128)
        try:
            said()
            await stop.wait()
        finally:
            relay.flush()
            front.close()                  # ★wait_closed は待たない（3.12 は線が全部閉じるまで返らない。線は内側の口を閉じると閉じる）


def _admin(port: int, op: str, email: str) -> int:
    """パスワードの再設定とアカウントの削除（推論機 の中から・手元の線でだけ中継が受ける）。"""
    import getpass
    from websockets.sync.client import connect
    msg: dict = {"op": op, "email": email}
    if op == "admin_reset":
        msg["password"] = getpass.getpass("新しいパスワード: ")
        if msg["password"] != getpass.getpass("もう一度: "):
            print("2 回の入力が違います", file=sys.stderr)
            return 2
    with connect(f"ws://127.0.0.1:{port}/auth", open_timeout=10) as ws:
        ws.send(json.dumps(msg, ensure_ascii=False))
        reply = json.loads(ws.recv(timeout=30))
    if reply.get("ok"):
        print("済みました。この人の端末はサインインし直しになります。")
        return 0
    print(reply.get("message") or reply.get("error") or "失敗しました", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="L.A.P.S relay (phones <-> every installed device)")
    ap.add_argument("--host", default=os.environ.get("LAPS_RELAY_HOST", "0.0.0.0"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("LAPS_RELAY_PORT", "18400")))
    ap.add_argument("--state", default=os.environ.get("LAPS_RELAY_STATE", os.path.expanduser("~/.local/share/laps/relay")))
    ap.add_argument("--reset-password", metavar="EMAIL", help="動いている中継にパスワードの再設定を頼む（手元からだけ）")
    ap.add_argument("--delete-account", metavar="EMAIL", help="動いている中継にアカウントの削除を頼む（手元からだけ）")
    a = ap.parse_args(argv)
    if a.reset_password:
        return _admin(a.port, "admin_reset", a.reset_password)
    if a.delete_account:
        return _admin(a.port, "admin_delete", a.delete_account)
    token = (os.environ.get("LAPS_RELAY_TOKEN") or "").strip() or None
    key = (os.environ.get("LAPS_RELAY_DEVICE_KEY") or "").strip() or None
    mode = (os.environ.get("LAPS_RELAY_AUTH") or "optional").strip().lower()
    if mode not in ("off", "optional", "required"):
        print(f"LAPS_RELAY_AUTH の値が違います: {mode}", file=sys.stderr)
        return 2
    signup = (os.environ.get("LAPS_RELAY_SIGNUP") or "").strip().lower()
    signup = signup if signup in ("open", "closed") else "first"
    state = Path(a.state)
    relay = Relay(token=token, device_key=key, state_dir=state, auth_mode=mode, signup=signup)
    tls = _tls_context(Path(os.environ.get("LAPS_RELAY_TLS_CERT") or state / "tls.crt"),
                       Path(os.environ.get("LAPS_RELAY_TLS_KEY") or state / "tls.key"))
    try:
        asyncio.run(run(relay, a.host, a.port, tls))
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
