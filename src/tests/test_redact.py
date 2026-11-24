# -*- coding: utf-8 -*-
"""記録を送る前の伏字門。**落とす側と落とさない側の両方**を固定する。

この門の失敗は2方向あって、片側だけ測ると必ずもう片側で壊れる:

  見逃し  資格情報がサーバへ出る。承認の条件そのものを破る。
  誤爆    記録が読めなくなる。「記録している」と思い込む分だけ、
          記録しないより悪い。

先行実装（Umbra の取り込み門 ~/umbra/ingest.py）は、伏字の判定を**前方一致**に
していたせいで `Password123!` を雛形と誤判定して**通していた**。回帰試験がそれを
捕まえている。ここでも同じ形の試験を置く。

★試験名は日本語。何を守っているかが一覧で読めるほうが、後から壊しにくい。
"""
from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from laps.redact import (
    MARK_OPEN,
    RULE_BY_KIND,
    RULES,
    SELFTEST_MUST_REDACT,
    redact,
    redact_obj,
    selftest,
)

SALT = b"test-salt-fixed"       # 指紋を決定的にする


def R(s: str):
    return redact(s, salt=SALT)


# =============================================================================
# 落とすべきものを落とす
# =============================================================================

# ★先行実装の回帰試験（gatetest.py）にあった「本物」をそのまま持ってきた。
#   あちらで実際に見逃していた値が入っている。ここで通ったら退行。
本物 = [
    "pass = Zx9-demo42",
    "SSHPASS=Qm7vTx2k",
    "api_key = sk_live_" + "9f3kQ2mZx8Lp",
    "Authorization: Bearer eyJhbGciOiJIUzI1",
    "password=Password123!",
    "token = ghp_" + "AbCdEfGh12345678",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    "passwd: Tr0ub4dor&3",
    "password = hunter2024x",
    "password=$ecretP4ss",
    "DB_PASSWORD=r3alS3cr3t",
    "MYSQL_ROOT_PASSWORD=Xk9$mQ2wL",
    "secret = a1b2c3d4e5f6g7h8",
]


@pytest.mark.parametrize("s", 本物)
def test_先行実装の回帰事例をすべて落とす(s):
    r = R(s)
    assert r.count >= 1, "見逃し: %r" % s
    assert MARK_OPEN in r.text


def test_先行実装が本物と誤って分類していた1件は落とさない():
    """★先行実装の「本物」一覧から**意図的に外した**1件。ここに理由を残す。

    Umbra の gatetest.py は `PASSWD:ALL**;` を本物のパスワードとして数えていた。
    知識ベース 119 ファイルへ当てて出処を辿ったところ、
    `/srv/notes/secret/security-findings.md` の

        - primary: **gameadmin = NOPASSWD:ALL**; Grafana/…

    だった。`**` は Markdown の**強調の閉じ**（開きはずっと左の `**gameadmin`）で、
    値は sudoers の構文 `ALL`。つまり**先行実装の回帰試験そのものに誤りがあり**、
    それに合わせると誤爆が固定される。落とさないのが正しい。

    ★これは「広い実データに当てたから見つかった」種類の誤りで、作り話の
      試験値をいくら並べても出てこない。
    """
    assert R("PASSWD:ALL**;").count == 0
    assert R("- primary: **gameadmin = NOPASSWD:ALL**; Grafana bound 0.0.0.0").count == 0
    # ただし本物が末尾に `**` を持っていても、伏せる範囲は元のまま（安全側）。
    # ★`**` を外すのは**判定に使う形**だけで、置き換えは一致した範囲そのもの。
    r = R("password=Kx7mQ2wLp**")
    assert r.count == 1 and "Kx7mQ2wLp" not in r.text and "**" not in r.text


def test_秘密鍵ブロックは中身ごと消える():
    body = (
        "鍵を貼ります:\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gt\n"
        "ZWQyNTUxOQAAACBQ9Xk2mJ0oQ3vZ5rT8yWpLnCfGhKdSaEbMxNvUwYzAiQ\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "以上です。\n"
    )
    r = R(body)
    assert r.kinds.get("private_key") == 1
    assert "b3BlbnNzaC1rZXktdjEA" not in r.text
    assert "BEGIN OPENSSH" not in r.text
    assert "鍵を貼ります" in r.text and "以上です" in r.text   # 前後は残る


def test_途中で切れた秘密鍵も本文を残さない():
    """★END が来ないまま記録が切れることがある（プロセスが落ちた・切り詰めた）。
    ここで本文が残ると、鍵の大半がそのまま出て行く。"""
    body = ("-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA3Zx8Qv2LmNpRt6YwUeAoIbCdKfShVjXnZqMwErTyUiOpAsDfGh\n")
    r = R(body)
    assert r.count == 1
    assert "MIIEpAIBAAKCAQEA" not in r.text


def test_途中で切れた秘密鍵が後続のログを巻き込まない():
    """★「END が無ければ末尾まで消す」は安全側だが、記録の後半を丸ごと失う。
    PEM の本文に空行は入らないので、空行で止める。"""
    body = ("-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA3Zx8Qv2LmNpRt6YwUeAoIbCdKfShVjXnZqMwErTyUiOpAsDf\n"
            "\n"
            "2026-08-11T10:22:31Z INFO 続きのログはここから。これは残ってほしい。\n")
    r = R(body)
    assert r.kinds.get("private_key") == 1
    assert "MIIEpAIBAAKCAQEA" not in r.text
    assert "続きのログはここから" in r.text


@pytest.mark.parametrize("s", [
    "sk-ant-api03-" + "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "AIza" + "SyD1a2B3c4D5e6F7g8H9i0J1k2L3m4N5o6P7q",
    "glpat-" + "x1Y2z3A4b5C6d7E8f9G0",
    "xoxb-" + "1234567890-abcdefghijkl",
    "hf_" + "QwErTyUiOpAsDfGhJkLzXcVbNm123456",
    "npm_" + "aB3dEfGhIjKlMnOpQrStUvWxYz012345",
])
def test_接頭辞で決まるトークンは文脈が無くても落ちる(s):
    """発行元が接頭辞を決めているので、値だけで判定できる。
    ログの真ん中に裸で落ちていても捕まえる。"""
    r = R("実行結果の途中に %s が出た" % s)
    assert r.count == 1, r.text
    assert s not in r.text


def test_JWTは三段の形で落ちる():
    tok = ("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
           ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4ifQ"
           ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c")
    r = R("cookie に %s が入っていた" % tok)
    assert r.count >= 1
    assert "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c" not in r.text


def test_接続文字列はパスワードだけを伏せて場所は残す():
    """★ホストとポートは障害調査に要る。パスワードだけ抜く。"""
    r = R("psql postgres://appuser:Tr0ub4dor3@db.internal:5432/appdb")
    assert r.kinds.get("url_password") == 1
    assert "Tr0ub4dor3" not in r.text
    assert "db.internal:5432/appdb" in r.text
    assert "appuser" in r.text


@pytest.mark.parametrize("cmd,秘密", [
    ("sshpass -p 'Qm7vTx2k' ssh admin@192.0.2.5", "Qm7vTx2k"),
    ('sshpass -p "Xk9mQ2wL" scp a b:/tmp', "Xk9mQ2wL"),
    ("mysql -uroot -pR34lSecret -e 'show databases'", "R34lSecret"),
    ("curl -u deploy:S3cr3tPass https://api.example.com", "S3cr3tPass"),
    ("aws configure --password Zx9Kq2Lm4Pw", "Zx9Kq2Lm4Pw"),
    ("gh auth login --token ghp_ZzYyXxWwVvUuTt11223344", "ghp_ZzYyXxWwVvUuTt11223344"),
])
def test_コマンド行の資格情報を落とす(cmd, 秘密):
    """★記録には「実行したコマンド」が入る。ここが素通りだと門の意味が無い。"""
    r = R(cmd)
    assert r.count >= 1, cmd
    assert 秘密 not in r.text


def test_Authorizationヘッダの値を落とす():
    r = R('curl -H "Authorization: Bearer abcDEF123456xyzQWE" https://x/y')
    assert r.count >= 1
    assert "abcDEF123456xyzQWE" not in r.text
    assert "Authorization: Bearer" in r.text      # 何のヘッダかは残る


# --- 日本語の文脈 -------------------------------------------------------------
@pytest.mark.parametrize("s,秘密", [
    ("パスワードは Zx9-demo42 です", "Zx9-demo42"),
    ("パスワード：Tr0ub4dor3", "Tr0ub4dor3"),
    ("パスワードが Xk9mQ2wL に変わっていました", "Xk9mQ2wL"),
    ("アクセストークン: ghp_AbCdEfGh12345678", "ghp_AbCdEfGh12345678"),
    ("合言葉は h0shizora99 でした", "h0shizora99"),
    ("認証情報＝Qm7vTx2k2024", "Qm7vTx2k2024"),
])
def test_日本語の文脈でも値を落とす(s, 秘密):
    """★英語の key=value しか見ない実装は、日本語の記録で素通りする。
    値が ASCII の秘密形のときだけ「は」「が」を区切りとして認める。"""
    r = R(s)
    assert r.count >= 1, s
    assert 秘密 not in r.text


def test_落としたことが本文から分かる():
    """★黙って消さない。何を落としたかが読めて、件数も出る。"""
    r = R("password=R34lSecret9 と token=ghp_ZzYyXxWwVvUuTt11223344")
    assert r.count == 2
    assert MARK_OPEN in r.text
    assert set(r.kinds) == {"password", "known_token"}
    assert sum(r.kinds.values()) == r.count


def test_同じ値は同じ指紋になり違う値は違う指紋になる():
    """★指紋があると「同じ間違ったパスワードを5回試していた」が値を見ずに分かる。
    塩は端末の外へ出ないので、送信物から値には戻れない。"""
    a = R("password=R34lSecret9").text
    b = R("passwd: R34lSecret9").text
    c = R("password=Different77").text
    fp = lambda t: t.split("/")[-1].rstrip("›")
    assert fp(a) == fp(b)
    assert fp(a) != fp(c)


def test_指紋を付けない設定なら印だけになる():
    r = redact("password=R34lSecret9", salt=None)
    assert r.count == 1
    assert "‹伏字:password›" in r.text


# =============================================================================
# 落としてはいけないものを落とさない（＝誤爆）
# =============================================================================

# ★先行実装の回帰試験にあった「例示」。過剰に弾くと解説文書がまるごと落ちる。
例示 = [
    "PGPASSWORD=${PGPASSWORD:?database password not set}",
    "password=*****,file_mode=0755",
    "PASSWORD=mysecretpassword",
    "password: your_password",
    "db-password:\n  file: db_password.txt",
    "PASSWORD=${DB_PASS}",
    "password: <YOUR_PASSWORD>",
    "$password = 'dbpass';",
    "MYSQL_PASSWORD: secret",
    "password = 'googleguy'",
    "passwd: changeme",
]


@pytest.mark.parametrize("s", 例示)
def test_伏字と雛形と例示は落とさない(s):
    r = R(s)
    assert r.count == 0, "過剰: %r → %r" % (s, r.text)


@pytest.mark.parametrize("s", [
    # ハッシュ・識別子。★どれも大小混在でない or 連続が短いので落ちない。
    "git checkout 9ba44b0e6f2c1d4a8b3e5f7c9d1a2b4c6e8f0a12",
    "sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "uuid=550e8400-e29b-41d4-a716-446655440000",
    # ★モデル名。この形（大小英数が混ざる長い名前）を無差別に弾く実装だと
    #   このプロジェクトのログは読めなくなる。最長連続は "Instruct" の8文字。
    "model=Qwen3-Coder-30B-A3B-Instruct-Q4_K_M",
    "/root/gguf/Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf を読み込み",
    "target x86_64-pc-windows-gnu でビルド",
    # 普通のコード
    "if password is None: raise ValueError('password required')",
    "self.api_key_header = 'X-Api-Key'",
    "const PASSWORD_MIN_LENGTH: usize = 12;",
    "def check_token(token): return token in self.valid",
    "// トークンの検証はここでは行わない",
    "settings.deny_patterns.append(r'rm -rf')",
    # 普通のログ
    "2026-08-11T10:22:31Z INFO  auth: login ok for user alice from 192.0.2.9",
    "GET /api/team/13/projects HTTP/1.1 200 1284",
    "systemd[1]: Started Umbra — 大規模データ保管兼参照システム (API).",
    # 普通の日本語
    "パスワードは変更してください",
    "パスワードを忘れた場合はオーナーに連絡する",
    "トークンの上限は 32768 です",
    "鍵は共有しない。鍵の置き場所は knowledge を見ること",
    "認証は鍵のみで、パスワード認証は無効になっている",
    # ふつうの URL（@ が無いので接続文字列ではない）
    "https://www.example.org/api/v1/status?limit=100",
    "http://198.51.100.1:8900/query に投げる",
    "ssh -p 2222 user@devbox",
])
def test_ふつうのコードとログと日本語は落とさない(s):
    r = R(s)
    assert r.count == 0, "過剰: %r → %r" % (s, r.text)


# ★ここから下は、リポジトリ 3470 ファイルへ実際に当てて**見つけた**誤爆。
#   作り話では1件も出てこなかったものばかりなので、消さないこと。
#   （71件 → 4件 まで落とした過程で、1件ずつ原因を潰して足した）
@pytest.mark.parametrize("s,由来", [
    ("password: bool = False,", "rich/prompt.py — 型注釈を値と読んでいた"),
    ("username, password = proxy_auth", "httpcore/socks_proxy.py — `_` を記号と数えていた"),
    ("username, password = request.url.username, request.url.password",
     "httpx/_client.py — 属性参照"),
    ("token = Token.Name.Builtin.SchemeFunction", "pygments/lilypond.py — 属性参照"),
    ('api_key=os.environ.get("BLACKWALL_API_KEY", "sk-no-key-required"),',
     "backend.py — 後戻りで `os.environ.ge` を値と読んでいた"),
    ('"passwd:gecos", "passwd:name", "passwd:dir"', "pygments/_scheme_builtins.py — 列挙子"),
    ("//   40 層 / n_head 40 / n_head_kv 8 / key=value=128", "fit.rs — 注釈"),
    ("    key: Mutex<Option<SpawnKey>>,", "sidecar.rs — 型注釈"),
    ("fn build_command(py: &Path, cwd: &Path, key: &SpawnKey) -> Command {",
     "sidecar.rs — 引数の型"),
    ("type FnNvmlDeviceGetHandleByIndexV2 = unsafe extern \"C\" fn();",
     "gpu.rs — CamelCase の識別子（数字が末尾に1箇所）"),
    ("COCOA_INTERFACES = {'NFCISO15693CustomCommandConfiguration', 'CICode128BarcodeGenerator'}",
     "pygments/_cocoa_builtins.py — 規格番号を含む API 名"),
    ('UNRESERVED = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"',
     "httpx/_urlparse.py — ★エントロピー最大なので閾値では落ちない"),
    ('"integrity": "sha512-VjhBRZaZ15zZFmZjHFMzZfPUL0ZKGYqxKAmw2A3g/dEXvNFm3TWr3RTBpVFV6b0Q=="',
     "package-lock.json — 内容ハッシュは秘密ではない"),
    ("# https://github.com/agronholm/anyio/issues/1204#issuecomment-2054437954",
     "anyio/_sockets.py — URL の後半が1つの長い連続に見えていた"),
    ("トークンは `secrets.token_urlsafe(32)` で生成され標準出力に1回だけ出る",
     "discover_convention.json — 値ではなく値の作り方"),
    ("Tests pass: `python3 greet.py --name=World --uppercase` outputs `HELLO`",
     "baseline_rep2.json — 引用されたコマンド行"),
    ("/etc/passwd:1\\n/etc/policy-rc.d:1\\n/etc/profile:1",
     "native_persona_60.json — ★JSON 内の `\\n` は2文字なので行が切れていなかった"),
    ("ssh -F /root/.ssh/recon_config user@blackwall", "remote_read.py — ssh の設定ファイル"),
    ("- 接続・鍵は `knowledge/credentials.md` / `ssh-key.md`（機微）",
     "git-session-folder.md — ★鍵の**在り処を書いた索引**であって鍵ではない"),
    ("運用自動化が使う資格情報は **`/root/recon/.review_creds`**（0600）に格納し",
     "infra-maintenance-mandate.md — Markdown の強調が値にくっついていた"),
    ("- パスワードは **creds.json に置かず** `/srv/notes/private/vcenter-api.cred` に保管",
     "Corridorの資格情報マネージャ.md — 強調が語をまたぐ形"),
])
def test_実測で見つけた誤爆を二度と出さない(s, 由来):
    r = R(s)
    assert r.count == 0, "%s\n  入力: %r\n  結果: %r" % (由来, s, r.text)


def test_sshの公開側のファイル名は残す():
    """★config や known_hosts まで伏せると ssh の障害調査ができなくなる。
    伏せるのは「どの秘密鍵か」だけ。"""
    for keep in ["~/.ssh/config", "~/.ssh/known_hosts", "/root/.ssh/authorized_keys",
                 "~/.ssh/id_rsa.pub"]:
        assert R(keep).count == 0, keep


def test_ssh秘密鍵のファイル名は伏せるが場所は残す():
    r = R("ssh -i /root/.ssh/umbrakey maxwell@198.51.100.1")
    assert r.kinds.get("ssh_path") == 1
    assert "umbrakey" not in r.text
    assert "/root/.ssh/" in r.text          # ssh 鍵の話だ、とは分かる


def test_弱い名前は秘密らしい形の値にしか当たらない():
    """★`key` `鍵` `auth` は普通の語でもある。ここを強い名前と同じ扱いにすると
    ふつうの文章が伏字だらけになる。"""
    assert R("鍵は umbrakey").count == 0            # 鍵の**名前**であって値ではない
    assert R("key: value").count == 0
    assert R("auth: none").count == 0
    assert R("token の数は 512").count == 0
    # 一方、明らかに値の形なら当たる
    assert R("key = a1B2c3D4e5F6g7H8").count == 1


# =============================================================================
# 再現率を測る（誤爆を絞った結果、落とすべきものまで落ちなくなっていないか）
# =============================================================================
# ★誤爆を減らす変更は、必ずどこかで見逃しを増やす。片側だけ測ると必ず壊れる。
#   このプロジェクトの記録に実際に混ざりうる形を並べてある。
実際に混ざりうる秘密 = [
    # ラボの資格情報（形だけ真似た架空の値）
    "ssh user@192.0.2.9 のパスワードは Kx7#mQ2wLp です",
    "sudo のパスワード: Yuk1_5174!",
    "smbclient //192.0.2.5/Share01 -U debian%Wq8zR2mN",
    "vCenter administrator@vsphere.local / P4ssw0rd-2026",
    'curl -H "X-Api-Key: 9fK2mZx8LpQ3vR7tYw1B" http://198.51.100.1:8900/note',
    "export UMBRA_TOKEN=aB3dEf9kQ2mZx8LpVwXy",
    "GENSPARK_API_KEY=sk-ant-api03-Kx7mQ2wLpZv9Rt3Yw1BnM4",
    # 環境変数・設定ファイル
    "PGPASSWORD=Str0ngP4ss",
    "MYSQL_ROOT_PASSWORD=Xk9$mQ2wL",
    "REDIS_PASSWORD='Zq4#Lm8Pw2'",
    "client_secret: GOCSPX-9fK2mZx8LpQ3vR7tYw1B",
    "aws_secret_access_key = wJalrXUtnFEMI7K9MDENG2bPxRfiCYE5XAMPLE",
    # 接続文字列
    "mongodb://admin:Sup3rS3cret@10.0.0.5:27017/db",
    "amqp://svc:Rb7#kQ2m@rabbit:5672/%2f",
    # ヘッダ・鍵
    "Authorization: Basic ZGVwbG95OlMzY3IzdFBhc3M=",
    "-----BEGIN EC PRIVATE KEY-----\nMHcCAQEEIB1\n-----END EC PRIVATE KEY-----",
    # 会話のなかで自然に出る形
    "さっき教えてもらったトークン ghp_Kx7mQ2wLpZv9Rt3Yw1BnM4Ab をまだ使っています",
    "設定ファイルに password=Tr0ub4dor&3 が直書きされていました",
]


def test_実際に混ざりうる秘密をすべて落とす():
    """★誤爆を 71 件 → 4 件へ絞る過程で除外規則を10個ほど足した。
    その全部が済んだあとで、落とすべき側が無事かをここで測る。"""
    見逃し = [s for s in 実際に混ざりうる秘密 if R(s).count == 0]
    assert not 見逃し, "見逃し %d/%d:\n%s" % (
        len(見逃し), len(実際に混ざりうる秘密), "\n".join(repr(s)[:80] for s in 見逃し))


def test_承知している取りこぼしを隠さない():
    """★できないことを「できる」と書かないための試験。

    誤爆を抑えるために入れた除外規則には、原理的な取りこぼしが伴う。
    ここが**通る**＝取りこぼしが仕様どおりであることの確認で、
    直したくなったらこの試験ごと直す。黙って穴が開くのを防ぐ。
    """
    取りこぼし = {
        # 全部小文字・記号なし・10文字未満（`gecos` `dir` を通すための代償）
        "password=hunter": "全小文字で短い値",
        # 全部小文字の snake_case（`proxy_auth` を通すための代償）
        "password=blue_horse_lamp": "全小文字の snake_case",
        # 本文中の引用された空白入り（`Tests pass: \`cmd args\`` を通すための代償）
        'password="two word phrase"': "本文中の空白入りの値",
        # 日本語のパスフレーズ（「パスワードは変更してください」を通すための代償）
        "パスワードは ひみつのあいことば です": "非 ASCII の値",
    }
    まだ落ちる = [s for s in 取りこぼし if R(s).count]
    assert not まだ落ちる, (
        "取りこぼしが解消している。良い変更かもしれないが、"
        "この試験と redact.py の ⚠ コメントを更新すること: %s" % まだ落ちる)
    # ★ただし**構造（JSON）経由なら空白入りは拾える**。記録の本体はこちらを通る。
    assert redact_obj({"password": "two word phrase"}, salt=SALT).count == 1


# =============================================================================
# 構造（JSON）
# =============================================================================

def test_JSONの鍵と値の結び付きで落とす():
    """★JSON の鍵と値は構造として結び付いているので、`password=…` と同じ形。
    語だけを見ているのではない。"""
    ev = {"type": "tool_call", "payload": {
        "name": "bash",
        "input": {"cmd": "ssh host", "password": "R34lSecret9"},
        "env": {"SSHPASS": "Qm7vTx2k2024", "PATH": "/usr/bin:/bin"},
    }}
    r = redact_obj(ev, salt=SALT)
    assert r.count == 2, r.text
    s = json.dumps(r.text, ensure_ascii=False)
    assert "R34lSecret9" not in s and "Qm7vTx2k2024" not in s
    assert "/usr/bin:/bin" in s               # 普通の値は残る
    assert r.text["type"] == "tool_call"


def test_値の側が語なだけなら落とさない():
    r = redact_obj({"note": "password", "label": "トークン", "kind": "secret"}, salt=SALT)
    assert r.count == 0, r.text


def test_入れ子と配列をたどる():
    ev = {"msgs": [{"role": "user", "content": "パスワードは Zx9-demo42 です"},
                   {"role": "assistant", "content": "了解しました"}]}
    r = redact_obj(ev, salt=SALT)
    assert r.count == 1
    assert "Zx9-demo42" not in json.dumps(r.text, ensure_ascii=False)
    assert r.text["msgs"][1]["content"] == "了解しました"


def test_文字列以外はそのまま通す():
    ev = {"n": 42, "ok": True, "none": None, "f": 1.5}
    r = redact_obj(ev, salt=SALT)
    assert r.count == 0 and r.text == ev


# =============================================================================
# 誤爆を実測する（作り話の入力ではなく、このリポジトリの実物で測る）
# =============================================================================

REPO = Path(__file__).resolve().parents[2]


def _実物を集める():
    files = []
    files += sorted((REPO / "src" / "laps").glob("*.py"))
    files += sorted((REPO / "src" / "tests").glob("*.py"))
    rust = REPO / "app" / "src-tauri" / "src"
    if rust.is_dir():
        files += sorted(rust.glob("*.rs"))
    # ★除外＝「資格情報らしい値を**わざと**持っているので、伏字が当たるのが正しい」ファイル。
    #   規則を緩める場所ではない（緩めると本番の出力から資格情報が漏れる）。
    #   test_remote_read_e2e.py は実接続の試験で、資格情報ファイルの**場所**を
    #   `/root/.ssh/bwverify.pass` と直書きしている（値ではなく経路）。ssh_path 規則が
    #   ここに当たるのは誤爆ではなく設計どおり＝この一覧に載せるのが正しい扱い。
    #   ★2026-10-18 追加: 秘密を見張る仕組みの試験 3 本（鍵らしい値・DB_PASSWORD=・秘密鍵の頭を**わざと**作り、
    #   伏せる・止める・読まないことを確かめる）。値は作り話（本物ではない）。
    除外 = {"redact.py", "test_redact.py", "test_remote_read_e2e.py", "test_redact_cred_siblings.py",
            "test_dev_harness_20261017.py", "test_memory_audit_20261016.py", "test_oc_tools_audit_20261016.py"}
    return [f for f in files if f.name not in 除外]


def test_実物のソースに当てて誤爆がないこと():
    """★誤爆は作り話の入力では見つからない。実際のコードで測る。

    redact.py と test_redact.py 自身は除く（本物そっくりの試験値を持っているので、
    落ちるのが正しい）。それ以外は資格情報を持たないはずなので、1件でも落ちたら
    それは誤爆——中身を見て規則を直すこと。
    """
    files = _実物を集める()
    assert len(files) >= 30, "対象が少なすぎて測ったことにならない: %d" % len(files)
    誤爆 = []
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        if f.suffix == ".rs":
            # ★Rust の単体試験（#[cfg(test)] の後ろ）は、秘密を伏せる関数を作り話の値で確かめている（voiceassist.rs の
            #   mask_secrets・2026-10-18 に赤で見つけた）。本体のコードだけを測る。
            text = text.split("#[cfg(test)]")[0]
        r = redact(text, salt=SALT)
        if r.count:
            for line in r.text.splitlines():
                if MARK_OPEN in line:
                    誤爆.append("%s: %s" % (f.name, line.strip()[:120]))
    assert not 誤爆, "誤爆 %d 件:\n%s" % (len(誤爆), "\n".join(誤爆[:20]))


def test_日本語の設計文書に当てて誤爆がないこと():
    """★Umbra は、語で弾いたせいで**有用な設計文書**を落とした。同じことをしない。"""
    docs = [REPO / "src" / "DESIGN.md", REPO / "src" / "REQUIREMENTS.md",
            REPO / "ACCEPTANCE.md", REPO / "WORKLOG.md"]
    docs = [d for d in docs if d.exists()]
    if not docs:
        pytest.skip("設計文書が無い")
    誤爆 = []
    for d in docs:
        r = redact(d.read_text(encoding="utf-8", errors="replace"), salt=SALT)
        for line in r.text.splitlines():
            if MARK_OPEN in line:
                誤爆.append("%s: %s" % (d.name, line.strip()[:120]))
    assert not 誤爆, "誤爆 %d 件:\n%s" % (len(誤爆), "\n".join(誤爆[:20]))


# =============================================================================
# 作業を止めない（速さ）
# =============================================================================

def _最小の一件あたり(f, n: int, 回=5) -> float:
    """★時間の試験は**最小値**で見る。

    平均だと、他のセッションが同じマシンで cargo と pytest を回しているだけで
    跳ねて落ちる（preflight はビルドの門なので、そこが時々落ちるのは害）。
    最小値なら、外乱は上にしか出ないので「速さの退行」だけを見られる。
    """
    最小 = float("inf")
    for _ in range(回):
        t = time.perf_counter()
        for _ in range(n):
            f()
        最小 = min(最小, (time.perf_counter() - t) / n * 1e6)
    return 最小


def test_一件のイベントの費用を測って上限を固定する():
    """★「作業を止めない」は測らないと守れない。実測（このマシン）:

        token イベント（92 B）        16 µs
        tool_result（2.5 KB）        646 µs
        大きめの出力（23 KB）        5.6 ms   ← 応答経路に置いてはいけない大きさ

    だから伏字は**送信器（背景）**で掛ける。会話の経路に挟むなら token の側だけ。
    ここは上限を固定して、規則を足したときに桁が変わったら気づけるようにする。
    """
    tok = {"ts": 1785.0, "seq": 3, "type": "token",
           "payload": {"text": "systemctl --failed を実行します。"}}
    us = _最小の一件あたり(lambda: redact_obj(tok, salt=SALT), 200)
    assert us < 200, "token イベント1件に %.0f µs（16 µs 前後のはず）" % us


def test_一万文字でも十分に速い():
    """★上位の要求は「会話の応答を 1ms も遅らせない」。門が遅いと、その要求は
    伏字を掛けない理由になってしまう。ここが下限を守る。"""
    body = ("2026-08-11T10:22:31Z INFO auth: login ok for user alice from 192.0.2.9\n"
            "systemctl --failed した結果 4 件。model=Qwen3-30B-A3B-Instruct-Q4_K_M\n") * 100
    assert len(body) > 10000
    ms = _最小の一件あたり(lambda: redact(body, salt=SALT), 5, 回=3) / 1000
    assert ms < 50, "1万文字に %.1f ms かかっている" % ms


@pytest.mark.parametrize("body", [
    "A" * 20000 + "=" + "B" * 20000,
    "password=" + "a" * 30000,
    "password:" + "ab" * 15000,
    "-----BEGIN RSA PRIVATE KEY-----\n" + "MIIE" * 8000,     # END が来ない
    "https://" + "u" * 5000 + ":" + "p" * 5000,              # @ が来ない
    ("key=" * 5000),
    "‹伏字:password/abcd›" * 3000,                            # 既に伏せた入力
])
def test_巨大で意地悪な入力でも指数時間にならない(body):
    """★正規表現の破滅的後戻りを踏むと、記録が会話を止める。

    値の量化子を所有的（`{3,200}+`）にしたのは誤爆対策だったが、
    後戻りを止める効果もある。ここで「止まらない」ことを固定しておく。
    """
    t = time.perf_counter()
    redact(body, salt=SALT)
    assert time.perf_counter() - t < 1.0, "%.2f 秒かかった" % (time.perf_counter() - t)


def test_二度掛けても壊れない():
    """★送信器が再送するとき、既に伏せた記録をもう一度通すことがある。
    印が二重になったり、印そのものを値と読んだりしないこと。"""
    once = R("password=Kx7#mQ2wLp と ghp_AbCdEfGh12345678")
    twice = R(once.text)
    assert twice.count == 0, twice.text
    assert twice.text == once.text


# =============================================================================
# アプリ（Rust）から使う口
# =============================================================================

PY = sys.executable


def _run(args, stdin=""):
    return subprocess.run([PY, "-m", "laps.redact"] + args, input=stdin,
                          capture_output=True, text=True, cwd=str(REPO / "src"))


def test_自己試験がその場で合否を返す():
    """★画面の文言を固定文字列で書かないため。実際に通した結果を出す。"""
    ok, bad = selftest()
    assert ok, bad
    p = _run(["--selftest"])
    assert p.returncode == 0, p.stdout + p.stderr
    assert json.loads(p.stdout)["ok"] is True


def test_規則の一覧を機械が読める形で出す():
    p = _run(["--rules"])
    assert p.returncode == 0
    rules = json.loads(p.stdout)
    assert {r["kind"] for r in rules} == {r.kind for r in RULES}
    assert all(r["why"] for r in rules)


def test_出しうる印はすべて画面で説明できる():
    """★「できると書いて、できないのが最悪」の逆側。
    印に載る kind に説明が無いと、画面は固定文字列を書くしかなくなる。
    ここが**実際に出る kind**を集めて、全部に説明があることを確かめる。
    """
    出た = set()
    for s in (本物 + 実際に混ざりうる秘密 + SELFTEST_MUST_REDACT
              + ["ssh -i ~/.ssh/umbrakey h", "見つけた鍵 aB3dEf9kQ2mZx8L1p2VwXyZ3"]):
        出た |= set(R(s).kinds)
    未説明 = 出た - set(RULE_BY_KIND)
    assert not 未説明, "説明の無い kind: %s" % 未説明
    # ★閾値を変えたのに説明文が古いまま、が起きない形になっているか
    from laps.redact import BLOB_MIN_RUN, BLOB_MIN_DIGIT_RUNS
    why = RULE_BY_KIND["high_entropy"].why
    assert str(BLOB_MIN_RUN) in why and str(BLOB_MIN_DIGIT_RUNS) in why


def test_濾し器として標準入出力で使える():
    """アプリはこれを呼べば同じ判定が使える（Rust に書き写さない）。"""
    p = _run([], stdin="password=R34lSecret9\nふつうの行\n")
    assert p.returncode == 0
    assert "R34lSecret9" not in p.stdout
    assert "ふつうの行" in p.stdout
    集計 = json.loads(p.stderr.strip().splitlines()[-1])
    assert 集計["count"] == 1 and 集計["kinds"] == {"password": 1}


def test_JSONLの濾し器は結果だけを標準出力に出す():
    src = json.dumps({"type": "tool_call", "payload": {"password": "R34lSecret9"}},
                     ensure_ascii=False)
    p = _run(["--jsonl"], stdin=src + "\n")
    assert p.returncode == 0
    out = json.loads(p.stdout.strip())
    assert out["type"] == "tool_call"
    assert "R34lSecret9" not in p.stdout
    assert json.loads(p.stderr.strip().splitlines()[-1])["count"] == 1


def test_壊れた行を黙って捨てない():
    """★静かな取りこぼしを作らない。JSON として読めなくても、伏字を掛けて通す。"""
    p = _run(["--jsonl"], stdin="{壊れた password=R34lSecret9\n")
    assert p.returncode == 0
    assert "R34lSecret9" not in p.stdout
    assert p.stdout.strip(), "行が消えている"


def test_cp932_の実機で濾し器が死なない():
    """★日本語 Windows（cp932）で `python -m blackwall.redact` が最初の1行で
    落ちていた（2026-09-02・再現して確認）:

        UnicodeDecodeError: 'cp932' codec can't decode byte 0x81 in position 25

    この経路はこのモジュールの docstring が「アプリから直接掛けたいとき」の
    正規の入口として案内しているもの。落ちた濾し器は「伏せなかった」のではなく
    **何も通さなかった**ので、呼ぶ側の作りによっては記録がまるごと消える。
    serve.py は同じ理由で `_fix_encodings()` を持っている＝**片方で直して
    片方に残していた**形（この現場で今日だけで 4 件出た族）。

    ★試験は環境変数で実機の符号化を作る（`PYTHONIOENCODING=cp932`）。
      プロセス全体の性質なので、別プロセスで測るしかない。
    """
    import os
    env = dict(os.environ, PYTHONIOENCODING="cp932")
    src = json.dumps({"text": "パスワードは Zx9-demo42 です"}, ensure_ascii=False)
    p = subprocess.run([PY, "-m", "laps.redact", "--jsonl"], input=src + "\n",
                       capture_output=True, text=True, encoding="utf-8",
                       cwd=str(REPO / "src"), env=env)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "Zx9-demo42" not in p.stdout, "伏せていない"
    assert "‹伏字:" in p.stdout, p.stdout
    assert "パスワードは" in p.stdout, "日本語が壊れている: " + p.stdout


# =============================================================================
# JSON の形（2026-09-20・敵対レビュー F15-1）
# =============================================================================

@pytest.mark.parametrize("text", [
    '{"user":"admin","password":"Zx9-demo42"}',
    '"password": "Zx9-demo42"',
    "'password': 'Zx9-demo42'",
    '"db_password":"Zx9-demo42",',
    '"client_secret" : "Zx9-demo42"',
], ids=["json-compact", "json-spaced", "py-dict", "prefixed-key", "space-before-colon"])
def test_JSONの形の鍵と値を伏せる(text):
    """★旧: 鍵と `:` の間に閉じ引用符が挟まる形が**丸ごと素通り**していた。記録も設定も API の本文も JSON が主。"""
    out = redact(text, salt=SALT).text
    assert "Zx9-demo42" not in out, out
    assert MARK_OPEN in out


@pytest.mark.parametrize("text", [
    '{"note": "password"}',           # 値の側が語
    '"password": null',
    '"password_hint": "誕生日"',        # 値が日本語の文
    '"key": "value"',                 # 弱い名前＋秘密らしくない値
], ids=["word-as-value", "null", "japanese-value", "weak-plain"])
def test_JSONの形でも秘密でない物は通す(text):
    assert MARK_OPEN not in redact(text, salt=SALT).text
