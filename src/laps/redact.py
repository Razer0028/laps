"""記録をサーバへ送る前に、機微な**値**だけを伏せる門。

オーナーの承認は「認証情報は送らない」ことが条件だった。記録には利用者の入力・
実行したコマンド・ファイルの断片が入るので、鍵やパスワードは**必ず混ざる**。
混ざらないように気をつける、では守れない（規律に委ねた安全は、仕組みが止めない
なら守られない——このプロジェクトが別の場所で実際に踏んでいる）。だから送信経路の
手前に門を置き、通る全部をここに通す。

★ 語で判定しない。値の形で判定する。

  これは本プロジェクトで**実際に失敗した**ところ。`password` という語を探す走査が、
  パスワードを含む文書に対して 0 件を返した。逆に 知識サーバー の取り込み門は初版で
  `\\bsshpass\\b` を弾いていたため、**導入パッケージの一覧**（"sshpass/curl/jq/..."）
  まで機微と誤判定して有用な設計文書を落とした。語は、有るのに無いと言い、無いのに
  有ると言う。見るべきは「値が付いている形」。

  したがって全ての規則は「秘密そのものの形」か「鍵と値が結び付いている形」を見る。
  `パスワード` と書いてあるだけの行は通す。`パスワードは Zx9-demo42` は落とす。

★ 既存の実装を再発明していない。

  知識サーバー の取り込み門 `<知識サーバー>/ingest.py` の `is_sensitive()` が
  同じ問題を先に解いていて、そこで**血を流して得た教訓**をここへ持ってきた:

    * 伏字・雛形は**全体一致**で見る。前方一致にすると `Password123!` のような
      本物になりうる値まで「伏字」と誤判定して通す（実際に穴が開いていた）。
    * `\\s*` は改行を跨ぐ。YAML の `db-password:` と**次の行**の `file: ...` を
      繋げて誤検知する。**同じ行の中だけ**を見る。
    * `$VAR` は定義上つねに参照であって値ではない。ただし裸の `$var` を無条件に
      参照とみなすと `$ecretP4ss` という**本物のパスワード**を通す。大文字始まりの
      ときだけ参照とみなす。
    * 公開ドキュメントの例示値（`hunter2` `mysecretpassword`）を弾くと解説文書が
      まるごと落ちる。ただし**完全一致でのみ**例外にする。部分一致は穴になる。

  違いは処分の仕方。知識サーバー は「機微なら文書ごと取り込まない」。ここは記録が目的
  なので**値だけを伏せて残りは残す**。落としたことは印と件数で必ず見えるようにする
  （静かな取りこぼしを作らない）。

★ 誤爆はこの門の主要な失敗モード。

  伏せ過ぎた記録は読めない＝記録しなかったのと同じで、しかも「記録している」と
  思い込む分だけ悪い。sha256 も git のコミットも UUID も
  `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M` も**伏せてはいけない**。

  ★作り話の入力では誤爆は見つからない。実データに当てて数えた（`--scan`）:

      リポジトリ 3,470 ファイル（Python/Rust/Markdown/JSON）  71 件 → **4 件**
      /srv/notes/secret 119 ファイル 14,006 行（日本語）   41 件 → **35 件**
                                    （うち機微指定でない文書は 2 件）

  71 → 4 の過程で潰した誤爆の原因は全部この下にコメントで残してある（型注釈・
  属性参照・CamelCase の API 名・URL・内容ハッシュ・Markdown の強調・JSON の
  `\\n` が2文字であること等）。**どれも作り話では出てこなかった。**

  ★逆向きも測っている。誤爆を絞る変更は必ずどこかで見逃しを増やすので、
    `test_実際に混ざりうる秘密をすべて落とす` が反対側の下限を守る（実際に
    smbclient の `-U 利用者%パスワード` と手順書の `利用者@領域 / パスワード`
    という2形式の見逃しがこれで見つかり、規則を足した）。

  ★先行実装の回帰試験に**誤り**が1件あった。`PASSWD:ALL**;` は本物として
    数えられていたが、出処を辿ると Markdown の強調が閉じただけの
    `**gameadmin = NOPASSWD:ALL**;` だった。合わせると誤爆が固定されるので
    意図的に外してある（`test_先行実装が本物と誤って分類していた1件は落とさない`）。

★ 速さ。

  会話の応答を 1ms も遅らせない、が上位の要求。ここは正規表現の逐次適用だけで、
  外部への問い合わせもファイル I/O も無い（指紋の塩だけ初回に1回読む）。実測:

      token イベント（92 B）        16 µs
      tool_result（2.5 KB）        646 µs
      大きめの出力（23 KB）        5.6 ms

  ★つまり**大きい記録は応答経路に置けない**。伏字は送信器（背景）で掛けること。
    「速いから経路に挟んでよい」ではない——挟んでよいのは token の側だけ。
    試験 `test_一件のイベントの費用を測って上限を固定する` が上限を守る。

## 使い方（Python）

    from laps.redact import redact, redact_obj
    r = redact(text)          # r.text / r.count / r.kinds
    o = redact_obj(event)     # dict/list を再帰的に。件数は o.count

## 使い方（アプリ = Rust 側）

  **Rust に書き写さない。** 2箇所に実装すると必ず乖離する（sidecar.rs の冒頭に
  同じ理由が書いてある。実際 2026-08-11 に握手・道具名・依頼型で3回食い違った）。

  1. 本筋: 伏字は**送信の直前**で掛ける。アプリは生の記録をローカルの控えに
     追記するだけにして、サーバへ運ぶのは Python の送信器にする。門が経路上に
     1つしか無い形にすれば、Rust 側の書き忘れが原理的に起こらない。
  2. アプリから直接掛けたいとき（画面に「N件伏せました」と出す等）は、
     同梱の pyruntime を**濾し器**として呼ぶ:

         python -m blackwall.redact --jsonl   # 標準入力 JSONL → 標準出力 JSONL
         python -m blackwall.redact           # 標準入力 生text → 標準出力 生text

     どちらも最後の集計を**標準エラー**へ1行 JSON で出す（標準出力は結果専用）:

         {"count": 3, "kinds": {"password": 2, "known_token": 1}, "lines": 120}

  3. 画面の文言は実装から作る。`python -m blackwall.redact --rules` が規則の一覧を
     JSON で返すので、「何を伏せるか」の説明を固定文字列で書かない。
     `--selftest` は既知の当たり/外れを実際に通して合否を返す（終了コード 0/1）ので、
     起動時に叩けば「効いている」を画面に出せる。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path

# ---- 印 ---------------------------------------------------------------------
# ★黙って消さない。何を落としたかが読めて、かつ本文と紛れない形にする。
#   ‹ › (U+2039/U+203A) はコードにもログにもまず出ない。JSON 文字列にそのまま入る。
MARK_OPEN = "‹伏字:"
MARK_CLOSE = "›"

# 伏字の印そのもの。★送信器が再送で二度掛けすることがあるので、印を値と読んで
#   入れ子にしないこと（PLACEHOLDER に印の形が入っている／`‹ ›` は値の文字クラス
#   の外なので裸の規則も当たらない）。試験 `test_二度掛けても壊れない` が守る。
MARK_RX = re.compile(r"‹伏字:[^›]{0,64}›")


def _mark(kind: str, value: str, salt: bytes | None) -> str:
    """伏字の印。★短い指紋を付ける。

    指紋があると「同じ値が何度も出ている」「昨日と違う値だ」が値を見ずに分かる。
    トラブルシューティングでこれが効く場面は多い（間違ったパスワードを5回試して
    いた、等）。塩は端末の外へ出ないので、4桁でも総当りで戻せない。
    塩が無いときは指紋を付けない（塩無しの短い指紋は短いパスワードなら戻せる）。
    """
    if salt is None:
        return MARK_OPEN + kind + MARK_CLOSE
    fp = hashlib.sha256(salt + value.encode("utf-8", "replace")).hexdigest()[:4]
    return MARK_OPEN + kind + "/" + fp + MARK_CLOSE


# ---- 塩（指紋用・端末の外へ出さない） ----------------------------------------
_SALT_CACHE: bytes | None = None


def _salt() -> bytes:
    """指紋の塩。~/.blackwall/redact.salt に置く。**送信物には決して入らない。**

    ファイルに置くのは、セッションを跨いで同じ値が同じ指紋になってほしいため
    （プロセス内乱数だと「昨日と同じパスワード」が見えない）。書けない環境では
    プロセス内の乱数に落ちる——指紋の相関が session 内に閉じるだけで、安全側。
    """
    global _SALT_CACHE
    if _SALT_CACHE is not None:
        return _SALT_CACHE
    from . import paths            # 置き場の規則（新しい ~/.laps・旧い ~/.blackwall が在ればそちら）を通す
    home = str(paths.home_dir())
    p = Path(home) / "redact.salt"
    try:
        if p.exists():
            _SALT_CACHE = p.read_bytes().strip()
            if _SALT_CACHE:
                return _SALT_CACHE
        p.parent.mkdir(parents=True, exist_ok=True)
        s = secrets.token_hex(16).encode()
        # 0600 で作る。読めてしまうと指紋から値を戻せる。
        # ★O_EXCL。送信器と本体が同時に初回起動すると、O_TRUNC では後勝ちで
        #   互いに違う塩を握り、同じ値の指紋が食い違う（相関が取れなくなる）。
        #   負けた側は書けた側の塩を読み直す。
        try:
            fd = os.open(str(p), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(s)
        except FileExistsError:
            s = p.read_bytes().strip() or s
        _SALT_CACHE = s
    except OSError:
        _SALT_CACHE = secrets.token_bytes(16)
    return _SALT_CACHE


# ---- 伏字・雛形の判定（知識サーバー の教訓をそのまま持ってきた部分） -----------------
# ★**全体一致**で見る。前方一致にすると `Password123!` を「伏字」と誤判定して通す。
PLACEHOLDER = re.compile(
    r"(?i)^(?:"
    r"【?伏字】?|x{3,}|\*{3,}|\.{3,}|…+|＊{3,}"          # 伏字そのもの
    r"|<[^>]*>|\{\{[^}]*\}\}|｛[^｝]*｝|〈[^〉]*〉"        # <placeholder> / {{tmpl}}
    # シェル/compose の変数展開は定義上つねに参照であって値ではない。
    # 裸の $VAR は**大文字始まりのときだけ**参照とみなす（`$ecretP4ss` は本物）。
    r"|\$\{[^}]*\}?.*|(?-i:\$[A-Z_][A-Z0-9_]*)"
    # ${VAR:?msg} を途中で切り出すと "?msg" が残る。? や - 始まりは展開の残骸。
    r"|[?][^\s]*.*|-[^\s]*"
    r"|[*x＊]{3,}.*"                                     # password=***** の右辺
    r"|your[_-]?\w*|example\w*|sample\w*|dummy\w*|changeme|redacted|null|none|true|false"
    r"|例|ここに\w*|自分の\w*|password|passwd|secret|token|apikey|api[_-]?key"
    r"|‹伏字:[^›]*›"                                     # 既に伏せた印（二重掛け）
    r")$")

# ★公開ドキュメントで「例」として使われる値。**完全一致でのみ**例外にする。
#   部分一致にすると本物を通す穴になる（知識サーバー が明示的に警告している）。
DOC_EXAMPLE_VALUES = {
    "dbpass", "dbpassword", "mypass", "mypassword", "pass", "passwd",
    "examplepass", "yourpassword", "your_password", "password123",
    "hunter2", "s3cret", "letmein", "abc123", "12345", "123456",
    "foo", "bar", "baz", "test", "testpass", "root", "admin", "guest",
    "googleguy", "correcthorsebatterystaple",
}
# 一覧を伸ばし続けないための一般則。値そのものが password/secret/passwd という語
# だけで出来ているものは公開文書の例示とみなす（mysecretpassword / testpasswd）。
# ★部分一致にしないこと。"Password123!" のような実在しうる値まで通してしまう。
DOC_EXAMPLE_RX = re.compile(
    r"^[a-z_]{0,12}(secret|password|passwd)[a-z_]{0,12}[0-9]{0,4}$", re.I)

# 右辺が明らかにファイルの場所なら、それは値ではなく置き場所（docker compose の
# secrets 記法 `password_file: /run/secrets/db` など）。
_PATHY = re.compile(r"^(?:[./~]|[A-Za-z]:[\\/])[^\s]*$")
# 先頭に `./` が無い相対パス（`knowledge/credentials.md`）も置き場所。
# ★`鍵は \`knowledge/credentials.md\`` を伏せていた——**鍵の在り処を書いた
#   索引**であって鍵ではない。秘密は `/` で区切られた語の連なりの形にならない。
_RELPATH = re.compile(r"^[\w.\-]+(?:/[\w.\-]+)+$")

# ★右辺が**コードの識別子**なら値ではない。3470 ファイルの実測で出てきた誤爆:
#     rich/prompt.py    `password: bool = False`            → 型注釈
#     httpx/_client.py  `username, password = request.url.username, …` → 属性参照
#     pygments/lilypond `token = Token.Name.Builtin.Scheme…` → 属性参照
#   ドット連結の識別子は**必ず**コード。単独の識別子は下の語彙で見る。
_DOTTED_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
# snake_case の識別子（`proxy_auth` `oauth_token`）。★数字が無いものだけ。
# `username, password = proxy_auth` を伏せていた（`_` を記号と数えていたため）。
_SNAKE_IDENT = re.compile(r"^[a-z]+(?:_[a-z]+)+$")
# 呼び出し式（`secrets.token_urlsafe(32)`）。値ではなく「値の作り方」。
# 引用符で囲まれていると `(` が値の内側に入るので、先読みでは防げなかった。
_CALL_EXPR = re.compile(r"^[A-Za-z_][\w.]*\(.*\)$")
_CLOSERS = {")": "(", "]": "[", "}": "{", "）": "（", "」": "「", "』": "『"}
_CODE_WORDS = {
    # 型・キーワード（`password: bool` `token: String` `secret: Optional`）
    "bool", "boolean", "str", "string", "int", "integer", "float", "double",
    "bytes", "bytearray", "char", "void", "object", "any", "optional", "union",
    "list", "dict", "set", "tuple", "array", "map", "vec", "option", "result",
    "usize", "isize", "i32", "u32", "i64", "u64", "f32", "f64", "text", "varchar",
    # ★`gameadmin = NOPASSWD:ALL` は sudoers の**構文**であって秘密ではない。
    #   知識ベースの監査結果メモを伏せていた。
    "all",
    "self", "cls", "this", "super", "nil", "undefined", "auto", "const", "let",
    "mut", "pub", "static", "final", "input", "output", "value", "field", "type",
    "name", "user", "username", "login", "host", "port", "path", "file", "data",
}


def _bare(value: str) -> str:
    """判定の前に、値にくっついた**飾り**を落とす。

    ★記録には Markdown が混ざる。`資格情報は **\\`/root/recon/.review_creds\\`**` を
      値として拾ったとき、先頭の `**` があるせいで「これはパス」の判定も
      「雛形」の判定も効かず、ふつうの手順書を伏せていた（知識ベース 119 ファイル
      14,006 行に当てて見つけた）。飾りを落としてから中身を見る。

    ★外し方は**前から1文字ずつ、対応する後ろがあれば一緒に**。理由が3つある:
      * 値が `*` や `` ` `` で**始まる**ことは無い（Markdown の装飾しかない）ので
        先頭は無条件に外してよい。
      * 末尾は無条件に外せない。`PASSWD:ALL**;` の `**` は対になっておらず、
        値の一部かもしれない（両端を無条件に削って実際に見逃しを作った）。
      * 強調は語をまたぐ（`**creds.json に置かず**`）ので「両端が同じ文字のときだけ」
        では外れない。実際にこれで手順書を伏せた。
      閉じ括弧も、開き括弧より多いときだけ落とす——そうしないと
      `secrets.token_urlsafe(32)` の `)` が取れて呼び出し式に見えなくなる。
    """
    v = value.strip()
    while v and v[0] in "*`_~\"'":
        c, v = v[0], v[1:]
        if v.endswith(c):
            v = v[:-1]
        v = v.strip()
    # ★閉じ側だけ残った強調（`**gameadmin = NOPASSWD:ALL**;` の `**`）。開き側は
    #   ずっと左にあるので値の中では対にならない。末尾の `*` の連なりは外す。
    #   ここは**判定に使う形**を整えるだけで、伏せる範囲は元の一致そのまま
    #   （`password=Secret**` は `Secret**` ごと伏せる）。だから安全側。
    v = v.rstrip(";,、。").rstrip("*").rstrip()
    while len(v) >= 2 and v[-1] in _CLOSERS and v.count(_CLOSERS[v[-1]]) < v.count(v[-1]):
        v = v[:-1]
    return v


def _looks_like_placeholder(value: str) -> bool:
    """本物ではない（伏字・雛形・例示・置き場所）なら True。"""
    v = _bare(value)
    if not v:
        return True
    if PLACEHOLDER.match(v):
        return True
    bare = v.lower()
    if bare in DOC_EXAMPLE_VALUES or DOC_EXAMPLE_RX.match(bare):
        return True
    if _PATHY.match(v) and ("/" in v[1:] or "\\" in v[2:]):
        return True
    if _RELPATH.match(v):
        return True
    return False


def _text_value_is_secret(value: str, *, strong: bool) -> bool:
    """本文（生テキスト）の中で `鍵 = 値` として拾った右辺が秘密か。

    ★構造（JSON）と違い、本文では**引用符の中身に空白が入る**と一気に外れる。
      実測で拾った誤爆はどれもこの形だった:
          Tests pass: `python3 greet.py --name=World --uppercase`
          トークンは `secrets.token_urlsafe(32)` で生成され
      引用された一続きの文やコマンド行は「値」ではない。空白を含むなら降りる。
      ⚠ 承知の取りこぼし: 空白入りのパスフレーズ（`password="two words"`）は
        本文経路では拾えない。構造経路（redact_obj）は鍵と値が構造で結び付いて
        いるので空白を許しており、記録の本体であるイベントはそちらを通る。
    """
    v = _bare(value)
    if any(c.isspace() for c in v):
        return False
    return not _looks_like_placeholder(value) and _value_is_secret_shaped(value, strong=strong)


def _entropy(s: str) -> float:
    """Shannon エントロピー(bit/char)。短い語や繰り返しを弾くために使う。"""
    if not s:
        return 0.0
    n = len(s)
    counts: dict[str, int] = {}
    for ch in s:
        counts[ch] = counts.get(ch, 0) + 1
    return -sum((c / n) * math.log2(c / n) for c in counts.values())


# ---- 鍵の名前（英語と日本語の両方） ------------------------------------------
# ★2段階に分ける。これが誤爆の量を決める一番大きなつまみ。
#   強い名前 = それが付いた値はまず秘密（password / token / api_key / パスワード）
#   弱い名前 = 秘密のこともあるが普通の語でもある（key / 鍵 / auth / pass）
#   弱い名前には値の形をより強く要求する（後述 _value_is_secret_shaped）。
_STRONG_KEY = (
    r"pass(?:word|wd|phrase)|secret|api[_-]?key|apikey|access[_-]?key|"
    r"secret[_-]?key|private[_-]?key|auth[_-]?token|access[_-]?token|"
    r"refresh[_-]?token|client[_-]?secret|bearer|sshpass|"
    r"パスワード|パスフレーズ|合言葉|秘密鍵|認証情報|資格情報|"
    r"アクセスキー|アクセストークン|アクセス[_ ]?トークン|認証キー|認証トークン"
)
# ★弱い名前から `パス` を外した。日本語の `パス` は圧倒的に **path** の意味で、
#   実際に app/src-tauri/src/agent.rs の「パスの…」という注釈を伏せてしまった。
_WEAK_KEY = (
    r"token|key|pass|auth|credential[s]?|pwd|"
    r"トークン|鍵|認証"
)
# 区切り。★同じ行の中だけ。`\s*` は使わない（改行を跨いで誤検知する）。
_SEP = r"[ \t　]*(?:[=:：＝]|=>|->)[ \t　]*"
# 日本語の助詞。★`の` と `を` は外した。「資格情報を渡す」「パスの扱い」のような
#   ふつうの文を key=value と読んでしまい、実際に REQUIREMENTS.md を伏せた。
#   残した `は` `が` も、**値が ASCII のときだけ**当たる（下の _VALUE_JA）。
_SEP_JA = r"[ \t　]*[はが][ \t　]*"

# 値の文字クラス。★除いた記号にはそれぞれ理由がある:
#   ( ) [ ]  … コード式（`api_key=os.environ.get(...)` を値と読んでいた）
#   = |      … `key=value=128` のような注釈（fit.rs で誤爆）
#   < > { }  … 型注釈と雛形（`key: Mutex<Option<SpawnKey>>` で誤爆）
#   , ; 、 。 … 値の終端
#   \        … ★記録は JSON なので、改行が `\n` という **2文字**で入る。
#              「同じ行の中だけ」という守りがそこで効かず、grep の出力
#              `/etc/passwd:1\n/etc/policy-rc.d:1\n…` を丸ごと1つの値として
#              飲み込んでいた（実測で見つけた）。
# ★値は**最長で1つに決める**。ふつうの `{3,200}` は後戻りするので、
#   `api_key=os.environ.get(` に対して「`(` の手前まで」ではなく
#   **1文字戻って `os.environ.ge` を値と読み、残った `t` で先読みを満たす**。
#   実際にこれで backend.py を伏せた。
#   所有的量化子（`{3,200}+`）でも直るが、あれは **Python 3.11 以降**で、
#   pyproject.toml が宣言している下限は 3.10。宣言と実装を食い違わせないため、
#   同じ効果を「次の1文字も値の文字なら不一致」という否定先読みで作る。
#   （同梱ランタイムは 3.12.10 なので実害は無かったが、宣言のほうを黙って
#     破るのは「できると書いて、できない」の一形態なので直した。）
_VALUE = (r"[^\s\"'`,;、。()\[\]<>{}=|\\]{3,200}(?![^\s\"'`,;、。()\[\]<>{}=|\\])"
          r"|\"[^\"\n]{3,200}\"|'[^'\n]{3,200}'|`[^`\n]{3,200}`")
# 日本語の助詞を区切りにするときは、値が **ASCII の並び**であることを要求する。
# 「パスワードは変更してください」を伏せないための唯一の防壁。
_VALUE_JA = r"[!-~]{4,200}(?![!-~])"
# 値の直後が `(` `[` なら、それは呼び出し・添字であって値ではない。
_NOT_CALL = r"(?![(\[])"

# ★強い名前は語境界を要求しない。`\b` を付けていたら `PGPASSWORD=…` を見逃した
#   （`G` と `P` の間に語境界が無い）。DB_PASSWORD は `_` で通っていたので
#   気づきにくい形の穴だった。
#   ただし直前が `/` `\` のときは**ファイル名**（`/etc/passwd:1` は grep の出力で
#   あって鍵と値ではない）。ここだけは除く。
# ★鍵の直後の**閉じ引用符**を許す（2026-09-20・敵対レビュー F15-1）。
#   旧: 区切りは鍵の直後に来る前提だったので、JSON の `"password": "値"`・Python の `'password': '値'` が
#   **丸ごと素通り**していた（鍵と `:` の間に `"` が挟まる）。記録も設定ファイルも API の本文も JSON が主で、
#   いちばんよく出る形がいちばん抜けていた。`redact_obj`（構造を辿る入口）は鍵名で見るので無事だったが、
#   **道具の結果の中の JSON**（設定ファイルを Read した本文など）は文字列なのでこちらを通る。
_KEY_CLOSE = r"[\"']?"
RX_KEYED_STRONG = re.compile(
    r"(?i)(?<![/\\])(?P<key>(?:%s))%s(?:(?P<sep>%s)(?P<val>%s)|(?P<sepja>%s)(?P<valja>%s))%s"
    % (_STRONG_KEY, _KEY_CLOSE, _SEP, _VALUE, _SEP_JA, _VALUE_JA, _NOT_CALL))
# ★弱い名前は前に英数字が来ないことを要求する（`turkey=` `monkey=` を弾く）。
RX_KEYED_WEAK = re.compile(
    r"(?i)(?<![A-Za-z0-9/\\])(?P<key>(?:%s))%s(?:(?P<sep>%s)(?P<val>%s)|(?P<sepja>%s)(?P<valja>%s))%s"
    % (_WEAK_KEY, _KEY_CLOSE, _SEP, _VALUE, _SEP_JA, _VALUE_JA, _NOT_CALL))


def _value_is_secret_shaped(value: str, *, strong: bool) -> bool:
    """値そのものの形が秘密らしいか。

    ★ASCII を要求する。資格情報は事実上つねに ASCII で、これを外すと
      「パスワード: 変更してください」のような**日本語の文**が全部値に見える。
      日本語のパスフレーズは取りこぼすが、伏せ過ぎて記録が読めなくなるより良い
      （未対応として承知のうえ。必要になったら別の形で足す）。

    強い名前が付いていれば 4 文字以上の実体で十分（短いパスワードは実在する）。
    弱い名前（`key` / `鍵` / `auth`）は普通の文章にも出るので、
    **8文字以上かつ英字と数字が混ざる**か、**20文字以上**を要求する。
    そうしないと「鍵は site_key」のような**鍵の名前**まで伏せてしまい、
    記録が読めなくなる（伏せ過ぎは記録しなかったのと同じ害）。
    """
    v = _bare(value)
    if len(v) < 4 or not v.isascii():
        return False
    # コードの識別子は値ではない（型注釈・属性参照・snake_case）。
    if (_DOTTED_IDENT.match(v) or _SNAKE_IDENT.match(v) or _CALL_EXPR.match(v)
            or v.lower() in _CODE_WORDS):
        return False
    has_d = any(c.isdigit() for c in v)
    has_u = any(c.isupper() for c in v)
    has_l = any(c.islower() for c in v)
    # ★`_` と `-` は記号に数えない。識別子のふつうの文字であって、
    #   「値らしさ」の証拠にならない（proxy_auth を伏せた原因）。
    has_sym = any(not (c.isalnum() or c in "_-") for c in v)
    if strong:
        # ★資格情報は「数字・大文字・記号のどれか」を持つか、そうでなければ長い。
        #   全部小文字で短い語（`gecos` `dir` `name`）は、実測ではコードの
        #   識別子や列挙子であって値ではなかった（pygments の "passwd:gecos" 等）。
        #   ⚠ 承知の取りこぼし: 全部小文字・記号なし・4〜9文字のパスワード
        #     （`hunter` `letmein`）はここを通る。よくある例示語は DOC_EXAMPLE_VALUES
        #     が別途受け止めるが、完全ではない。伏せ過ぎで記録を潰すより取りこぼす
        #     側に倒した判断で、変えるならここ1箇所。
        return has_d or has_u or has_sym or len(v) >= 10
    # 弱い名前（key / 鍵 / auth）は普通の文章にも出るので、値の形をより強く要求する。
    # そうしないと「鍵は site_key」のような**鍵の名前**まで伏せて記録が読めなくなる。
    if len(v) >= 20:
        return True
    return len(v) >= 8 and has_d and (has_u or has_l)


# ---- 形そのものが秘密のもの（文脈が要らない） --------------------------------
# 発行元が接頭辞を決めているので、これらは値だけで判定できる。文脈は要らない。
RX_KNOWN_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_\-])(?:"
    r"sk-ant-[A-Za-z0-9_\-]{16,}"                 # Anthropic
    r"|sk-(?:proj-)?[A-Za-z0-9_\-]{20,}"          # OpenAI
    r"|(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{10,}"   # Stripe 系
    r"|gh[pousr]_[A-Za-z0-9]{16,}|github_pat_[A-Za-z0-9_]{20,}"  # GitHub
    r"|glpat-[A-Za-z0-9_\-]{16,}"                 # GitLab
    r"|xox[baprsе]-[A-Za-z0-9\-]{10,}"            # Slack
    r"|AKIA[0-9A-Z]{16}|ASIA[0-9A-Z]{16}"         # AWS アクセスキー ID
    r"|AIza[A-Za-z0-9_\-]{35}"                    # Google API キー
    r"|hf_[A-Za-z0-9]{20,}"                       # Hugging Face
    r"|bwd_[A-Za-z0-9_\-]{20,}"                   # Blackwall の中継の認証情報（relay.py）
    r"|npm_[A-Za-z0-9]{20,}|dckr_pat_[A-Za-z0-9_\-]{20,}"
    r"|SG\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}"  # SendGrid
    r"|shpat_[A-Za-z0-9]{20,}|EAACEdEose0cBA[A-Za-z0-9]+"
    r")(?![A-Za-z0-9_\-])")

# JWT。3つの base64url が . で繋がる形。ヘッダは必ず {" で始まるので eyJ 固定。
RX_JWT = re.compile(
    r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")

# 秘密鍵ブロック。★END が無い（途中で切れた）ときも本文を残さない。
RX_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----")
# END が来ないまま切れた鍵。★空行までで止める。
#   「行末まで」だと安全側だが、記録が途中で切れたときに**その後ろの無関係な
#   ログを丸ごと消す**。PEM の本文に空行は入らないので、空行が来たら鍵は終わり。
#   （切り詰めで末尾に来ている場合はそのまま末尾まで消える＝望みどおり。）
RX_PRIVATE_KEY_OPEN = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?(?=\n[ \t]*\n|\Z)")
# PuTTY / PGP も同じ扱い
RX_PRIVATE_KEY2 = re.compile(
    r"PuTTY-User-Key-File-\d[\s\S]*?(?=\n\s*\n|\Z)"
    r"|-----BEGIN PGP PRIVATE KEY BLOCK-----[\s\S]*?-----END PGP PRIVATE KEY BLOCK-----")

# Authorization ヘッダ。curl の -H "..." の中でも効く。
RX_AUTH_HEADER = re.compile(
    r"(?i)(?P<key>\b(?:proxy-)?authorization[ \t]*:[ \t]*"
    r"(?:bearer|basic|token|apikey)[ \t]+)(?P<val>[A-Za-z0-9._\-+/=~]{8,})")

# 接続文字列の中のパスワード。scheme://user:pass@host の pass だけを伏せる。
# ★`@` が要るので https://example.com:8080/path には当たらない。
RX_URL_PASSWORD = re.compile(
    r"(?P<pre>\b[a-z][a-z0-9+.\-]{1,20}://[^\s:/@\"']{1,64}:)(?P<val>[^\s/@\"'`]{1,128})(?P<post>@)")

# コマンド行の資格情報。★記録には「実行したコマンド」が入るので、ここが効く。
RX_CMD_CRED = re.compile(
    r"(?i)(?P<key>"
    r"\bsshpass[ \t]+-p[ \t]*"                       # sshpass -p secret
    r"|\bmysql(?:dump|admin)?[ \t][^\n|;]{0,80}?[ \t]-p"   # mysql -uroot -psecret
    r"|--(?:password|pass|token|api-key|apikey|secret|auth-token|client-secret)[ \t=]"
    # ★`\b-u` は当たらない。空白と `-` はどちらも非単語文字なので語境界が無い。
    #   これで `curl -u deploy:S3cr3tPass` を見逃していた。
    r"|(?<![A-Za-z0-9-])-u[ \t]+[^\s:]{1,64}:"       # curl -u user:pass
    # ★smbclient / net / mount.cifs の `-U 利用者%パスワード`。
    #   このラボは Samba を日常的に使うので、ここが素通りだと実害が出る
    #   （再現率の試験で実際に見逃していた）。`%` を要求するので誤爆しにくい。
    r"|(?<![A-Za-z0-9-])-U[ \t]*[^\s%'\"]{1,64}%"
    r")(?P<val>'[^'\n]{1,200}'|\"[^\"\n]{1,200}\"|[^\s'\"|;&)]{1,200})")

# sudo へ標準入力でパスワードを渡す形（2026-10-31: 開発機 の sudo パスワードがこの形の命令から 作業用のコンテナ の記録へ広がった。規則が無かった）。
#   sudo -S へ渡す物はパスワードなので、値の形は問わない。JSON の中の \\" にも当てる。
_SUDO_VAL = r"""(?:'(?P<val>[^'\n]{1,200})'|\\?"(?P<valq>[^"\\\n]{1,200})\\?"|(?P<valb>[^\s'"\\|;&)]{1,200}))"""
_SUDO_S = r"\bsudo\b[^\n|;&]{0,200}?[ \t]-[A-Za-z]*S\b"
RX_SUDO_HERE = re.compile(r"\bsudo\b[^\n]{0,400}?[ \t]-[A-Za-z]*S\b[^\n]{0,800}?<<<[ \t]*" + _SUDO_VAL)
RX_SUDO_PIPE = re.compile(r"""\b(?:echo|printf)[ \t]+(?:-[a-zA-Z]+[ \t]+|'%s[^']*'[ \t]+|\\?"%s[^"]*"[ \t]+)*"""
                          + _SUDO_VAL + r"[ \t]*\|[ \t]*" + _SUDO_S)

# 「利用者 / パスワード」という書き方。手順書と引き継ぎメモの定番で、
# `administrator@vsphere.local / P4ssw0rd-2026` のように**鍵の名前が出てこない**。
# ★語が無いので、値の形と「主体 / 値」という並びだけで判断するしかない。
#   誤爆を避けるため、値から `/`（パス）と `@`（メールアドレス）を外し、
#   数字と区切りだけの並び（日付・電話番号・版番号）も外す。
RX_PAIRED_CRED = re.compile(
    r"(?<![\w@.\-])[A-Za-z0-9._\-]{3,64}@[A-Za-z0-9.\-]{2,64}[ \t]*/[ \t]*"
    r"(?P<val>[\x21-\x2e\x30-\x3f\x41-\x7e]{6,64})(?![^\s])")
_NUMBERISH = re.compile(r"^[\d][\d.\-:]*$")       # 日付・電話番号・版番号

# .ssh 配下のファイル。★鍵そのものではないが「どの鍵か」を送らない。
#   .ssh/ は残す（ssh の鍵の話だ、と分かるほうが直しやすい）。葉だけ伏せる。
#   ★既定の鍵名（id_rsa 等）は伏せない。どの環境にもある名前で、伏せても
#     何も守らないのに、ssh の話がまるごと読めなくなる（実際に
#     tests/test_*.py の `cat ~/.ssh/id_rsa` を伏せて気づいた）。
#     守りたいのは `site_key` `prod_deploy` のような**その環境固有の名前**。
_SSH_PUBLIC_LEAF = {"config", "known_hosts", "known_hosts2", "authorized_keys",
                    "authorized_keys2", "environment", "rc",
                    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "id_ed25519_sk"}
#   `recon_config` のような **設定ファイル**も伏せない。伏せると
#   `ssh -F /root/.ssh/‹伏字›` になって、記録した手順が再現できなくなる。
_SSH_PUBLIC_SUFFIX = ("config", "known_hosts", ".pub", ".conf")
RX_SSH_PATH = re.compile(
    r"(?P<pre>(?:^|(?<=[\s\"'`=:(]))(?:~|\.{1,2}|/[^\s\"'`:]*|[A-Za-z]:\\[^\s\"'`:]*)?"
    r"[/\\]\.ssh[/\\])(?P<val>[A-Za-z0-9._\-]+)")

# 裸の高エントロピー文字列（接頭辞の無い API キー風）。
# ★ここが誤爆の主戦場で、条件を1つずつ実測で足した。
#
#   条件1 連続24文字以上（`_` `-` で切る）
#         → `Qwen3-Coder-30B-A3B-Instruct-Q4_K_M`（最長 "Instruct" 8字）や
#           `x86_64-pc-windows-gnu`、snake_case の識別子が落ちない。
#   条件2 大文字・小文字・数字がすべて有る
#         → sha256（小文字16進）・git のコミット・UUID が落ちない。
#   条件3 **数字の塊が3箇所以上**（連続した数字は1箇所と数える）
#         → CamelCase の識別子が落ちない。ここが一番効いた条件で、
#           実測で見つけた誤爆はどれも「数字が1箇所に固まっている」形だった:
#             FnNvmlDeviceGetHandleByIndexV2         (gpu.rs・版番号 V2)
#             NFCISO15693CustomCommandConfiguration  (Cocoa の API 名)
#             CICode128BarcodeGenerator              (同上)
#             ABCDE…xyz0123456789-._~                (URL の使用可能文字表)
#           識別子の数字は版番号・規格番号として1箇所に固まる。乱数由来の値は
#           散らばる（base62 で 24 字なら塊が期待値 3.7 箇所）。
#           ★「数字が N 個以上」では足りなかった。NFCISO15693… は数字5個ある。
#           ★最後の URL 文字表は**エントロピーが最大**（全文字が1回ずつ）なので、
#             エントロピーの閾値をいくら上げても落ちない。塊の数だけが効いた。
#   条件4 エントロピー 3.5 bit/char 以上
#
#   ⚠ 承知している取りこぼし: 数字の塊が2箇所以下の乱数値（base62 で 24 字なら
#     おおよそ 3 割）はここを通る。接頭辞つき（known_token）と鍵つき（password）
#     の2規則が本命で、ここは**保険**。無差別に伏せるより取りこぼす側に倒している。
RX_BARE_BLOB = re.compile(r"(?<![A-Za-z0-9+/=_\-])[A-Za-z0-9+/=_\-]{24,}(?![A-Za-z0-9+/=_\-])")
# ★連続の区切りに `/` を入れた。入れる前は URL の後半
#   （`github.com/agronholm/anyio/issues/…`）が1つの長い連続として当たり、
#   3470 ファイルの実測で高エントロピー誤爆の大半が URL だった。
#   代償として `/` を含む base64 の秘密は分割されて閾値を下回ることがある——
#   この規則はもともと保険で、本命は接頭辞つきと鍵つきの2規則。
_RUN = re.compile(r"[A-Za-z0-9+=]+")
_DIGIT_RUN = re.compile(r"\d+")      # 連続した数字は1つの塊と数える
# ★閾値は名前を付けて1箇所に置く。画面に出す説明文をここから組み立てるので、
#   値を変えたのに説明が古いまま、が起こらない（「できると書いて、できないのが
#   最悪」——固定文字列で説明を書かないための仕掛け）。
BLOB_MIN_RUN = 24        # 区切り無しの連続の長さ
BLOB_MIN_DIGIT_RUNS = 3  # 数字の塊の数
BLOB_MIN_ENTROPY = 3.5   # bit/char
# 内容ハッシュは秘密ではない（npm の package-lock / SRI の integrity）。
_CONTENT_HASH = re.compile(r"(?i)^(?:sha\d{3}|sha1|md5)-")
# ★直前が `SHA256:` なら鍵の**指紋**であって鍵ではない。指紋は公開情報で、
#   伏せると `ssh-keygen -lf` の出力が読めなくなるだけ（知識ベースで誤爆した）。
#   値そのものからは区別できないので、ここだけ直前の16文字を見る。
_CHECKSUM_CONTEXT = re.compile(
    r"(?i)(?:sha-?\d{1,3}|md5|sha|digest|checksum|fingerprint|integrity)\s*[:=]\s*$")


def _bare_blob_is_secret(tok: str) -> bool:
    if _CONTENT_HASH.match(tok):
        return False
    runs = _RUN.findall(tok)
    if not runs:
        return False
    longest = max(runs, key=len)
    if len(longest) < BLOB_MIN_RUN:
        return False
    if not (any(c.islower() for c in longest) and any(c.isupper() for c in longest)):
        return False
    if len(_DIGIT_RUN.findall(longest)) < BLOB_MIN_DIGIT_RUNS:
        return False
    return _entropy(longest) >= BLOB_MIN_ENTROPY


# ---- 規則表 -----------------------------------------------------------------
# ★画面の文言はここから作る。固定文字列を書かないための一覧でもある
#   (`python -m blackwall.redact --rules`)。
#: 秘密の鍵の**接頭辞**を取り出す（`MT5_PASSWORD` → `MT5`・`DB_ROOT_PASSWORD` → `DB_ROOT`）。
RX_SECRET_KEY_PREFIX = re.compile(
    r"(?<![A-Za-z0-9_])(?P<pref>[A-Za-z][A-Za-z0-9]*(?:_[A-Za-z0-9]+)*?)_"
    r"(?:PASSWORD|PASSWD|PASSPHRASE|PASS|PWD|SECRET|TOKEN|API_?KEY|ACCESS_?KEY|PRIVATE_?KEY)"
    r"(?![A-Za-z0-9])", re.IGNORECASE)
#: 兄弟として伏せる末尾。★HOST / URL / PORT は入れない（障害の切り分けに要り、単体では誰のものか分からない）。
_SIBLING_LEAF = "LOGIN|LOGIN_ID|USER|USERNAME|USER_NAME|USER_ID|ACCOUNT|ACCOUNT_ID|ACCOUNT_NO|ID|SERVER|SERVER_NAME"


@dataclass(frozen=True)
class Rule:
    kind: str
    why: str          # 何を見ているか（値の形の説明・日本語）


RULES: list[Rule] = [
    Rule("private_key", "秘密鍵ブロック（BEGIN…PRIVATE KEY）。END が無くても本文を残さない"),
    Rule("known_token", "発行元が接頭辞を決めているトークン（sk-ant- / ghp_ / AKIA… 等）"),
    Rule("jwt", "JWT（eyJ で始まる3つの base64url がドットで繋がった形）"),
    Rule("auth_header", "Authorization: Bearer/Basic に続く値"),
    Rule("url_password", "接続文字列 scheme://利用者:パスワード@ホスト のパスワード部"),
    Rule("cmd_cred", "コマンド行の資格情報（sshpass -p / curl -u u:p / smbclient -U u%p / --password 等）"),
    Rule("sudo_stdin", "sudo -S へ標準入力で渡すパスワード（sudo -S … <<< 'pw' / echo 'pw' | sudo -S）"),
    Rule("paired_cred", "手順書の「利用者@領域 / パスワード」という並び（鍵の名前が出てこない形）"),
    Rule("password", "強い名前（password / token / api_key / パスワード…）に値が付いた形"),
    Rule("weak_keyed", "弱い名前（key / 鍵 / auth…）に、秘密らしい形の値が付いた形"),
    Rule("ssh_path", "~/.ssh 配下のファイル名（どの鍵かを送らない。.ssh/ と既定名は残す）"),
    # ★説明文を実装の定数から組み立てる。閾値だけ変えて説明が古いまま、を防ぐ。
    Rule("high_entropy",
         "接頭辞の無い長い乱雑文字列（区切り無しで連続%d字以上・大小英字混在・"
         "数字の塊が%d箇所以上・エントロピー%.1fbit/char 以上）"
         % (BLOB_MIN_RUN, BLOB_MIN_DIGIT_RUNS, BLOB_MIN_ENTROPY)),
]
# 印に入る kind から説明を引く。画面はこれを使い、固定文字列を持たない。
RULE_BY_KIND = {r.kind: r for r in RULES}


# ---- 結果 -------------------------------------------------------------------
@dataclass
class RedactResult:
    text: str
    count: int = 0
    kinds: dict[str, int] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.count == 0


def _bump(kinds: dict[str, int], kind: str) -> None:
    kinds[kind] = kinds.get(kind, 0) + 1


def redact(text: str, *, salt: bytes | None = ..., fingerprint: bool = True) -> RedactResult:
    """機微な**値**を伏せる。返るのは伏せた本文と、何をいくつ落としたか。

    salt: 指紋の塩。既定は端末ごとの塩（~/.blackwall/redact.salt）。
          None を渡すと指紋を付けない。試験は固定の塩を渡して決定的にする。
    """
    if not isinstance(text, str) or not text:
        return RedactResult(text if isinstance(text, str) else "", 0, {})
    if salt is ...:
        salt = _salt() if fingerprint else None

    kinds: dict[str, int] = {}

    def sub(rx: re.Pattern, kind: str, s: str,
            *, group: str | tuple[str, ...] | None = None, guard=None) -> str:
        def rep(m: re.Match) -> str:
            g = group
            if isinstance(g, tuple):
                # 選択肢の regex（記号の区切り／日本語の助詞）。当たったほうを使う。
                g = next((n for n in g if m.group(n) is not None), None)
                if g is None:
                    return m.group(0)
            val = m.group(g) if g else m.group(0)
            if guard is not None and not guard(m, val):
                return m.group(0)
            _bump(kinds, kind)
            marked = _mark(kind, val, salt)
            if g is None:
                return marked
            # 値の位置だけ差し替える（前後の文脈は残す＝読めるようにするため）
            start, end = m.span(g)
            return m.group(0)[: start - m.start()] + marked + m.group(0)[end - m.start():]
        return rx.sub(rep, s)

    out = text

    # 1. 鍵ブロックが最初。中の base64 が他の規則に散らばって当たるのを防ぐ。
    out = sub(RX_PRIVATE_KEY, "private_key", out)
    out = sub(RX_PRIVATE_KEY2, "private_key", out)
    out = sub(RX_PRIVATE_KEY_OPEN, "private_key", out)   # END が無く途中で切れたもの

    # 2. 形だけで決まるもの
    out = sub(RX_KNOWN_TOKEN, "known_token", out)
    out = sub(RX_JWT, "jwt", out)
    out = sub(RX_AUTH_HEADER, "auth_header", out, group="val",
              guard=lambda m, v: not _looks_like_placeholder(v))

    # 3. 結び付きで決まるもの
    out = sub(RX_URL_PASSWORD, "url_password", out, group="val",
              guard=lambda m, v: not _looks_like_placeholder(v))
    for rx in (RX_SUDO_HERE, RX_SUDO_PIPE):
        out = sub(rx, "sudo_stdin", out, group=("val", "valq", "valb"),
                  guard=lambda m, v: not _looks_like_placeholder(v))
    out = sub(RX_CMD_CRED, "cmd_cred", out, group="val",
              guard=lambda m, v: not _looks_like_placeholder(v)
              and _value_is_secret_shaped(v, strong=True))
    out = sub(RX_PAIRED_CRED, "paired_cred", out, group="val",
              guard=lambda m, v: not _looks_like_placeholder(v)
              and not _NUMBERISH.match(v)
              and _value_is_secret_shaped(v, strong=False))
    out = sub(RX_KEYED_STRONG, "password", out, group=("val", "valja"),
              guard=lambda m, v: _text_value_is_secret(v, strong=True))
    out = sub(RX_KEYED_WEAK, "weak_keyed", out, group=("val", "valja"),
              guard=lambda m, v: _text_value_is_secret(v, strong=False))

    # 3.5 資格情報の**兄弟**（2026-09-21・友人の実機の記録から）。
    #   `.env` を読んだ結果が記録に乗り、`MT5_PASSWORD` は伏せたが **`MT5_LOGIN`（口座番号）と
    #   `MT5_SERVER` は素のまま**残った。単体では秘密の形をしていないが、**同じ接頭辞の秘密と
    #   並んでいるなら資格情報の一部**（口座番号＋サーバ名＝どこの誰の口座か）。
    #   ★語だけでは伏せない（`LOGIN=` をどこでも伏せると誤爆する）。**この本文で同じ接頭辞の
    #     秘密を実際に伏せたとき**だけ、その接頭辞の兄弟を伏せる。
    #   ★**既に伏字の印になっている**本文でも効かせる（保存済みの記録を後から伏せ直すため。
    #     最初に試したとき、パスワードが印になっていて「秘密を伏せた」に当たらず、兄弟が素通りした）。
    if (kinds.get("password") or kinds.get("weak_keyed") or kinds.get("known_token")
            or "‹伏字:" in text):
        for pref in sorted({m.group("pref") for m in RX_SECRET_KEY_PREFIX.finditer(text)},
                           key=len, reverse=True)[:8]:
            rx = re.compile(
                r"(?<![A-Za-z0-9_])%s_(?:%s)[\"']?\s*[=:]\s*\\?[\"']?(?P<val>[^\s\"'\\,;]+)"
                % (re.escape(pref), _SIBLING_LEAF), re.IGNORECASE)
            out = sub(rx, "cred_sibling", out, group="val",
                      guard=lambda m, v: not _looks_like_placeholder(v) and "‹" not in v)

    # 4. 場所（どの鍵か）
    out = sub(RX_SSH_PATH, "ssh_path", out, group="val",
              guard=lambda m, v: v.lower() not in _SSH_PUBLIC_LEAF
              and not v.lower().endswith(_SSH_PUBLIC_SUFFIX))

    # 5. 最後に裸の乱雑文字列。★ここまでで印になった部分には当たらない
    #    （印は ‹ › を含み、文字クラスの外にある）。
    out = sub(RX_BARE_BLOB, "high_entropy", out,
              guard=lambda m, v: _bare_blob_is_secret(v)
              and not _CHECKSUM_CONTEXT.search(m.string[max(0, m.start() - 16):m.start()]))

    return RedactResult(out, sum(kinds.values()), kinds)


# キーの名前が資格情報を指す形か（JSON の構造は「鍵と値の結び付き」そのものなので、
# ここで名前を見るのは「語で判定する」ではなく「値が付いている形を見る」に当たる）。
# 強い名前は**部分一致**（DB_PASSWORD / anthropic_api_key / X-Auth-Token）。
_KEY_NAME_STRONG = re.compile(r"(?i)(?:%s)" % _STRONG_KEY)
# 弱い名前は**完全一致**だけ（`key` は当てるが `keyboard_layout` は当てない）。
_KEY_NAME_WEAK = re.compile(r"(?i)^(?:%s)$" % _WEAK_KEY)


def redact_obj(obj, *, salt: bytes | None = ..., fingerprint: bool = True) -> RedactResult:
    """dict / list を再帰的に伏せる。イベント記録はこちらを使う。

    ★JSON の鍵と値は**構造として結び付いている**ので、`{"password": "hunter2"}` は
      `password=hunter2` と同じ形として扱う（語だけを見ているのではない）。
      `{"note": "password"}` は値の側が語なので落ちない。
    """
    if salt is ...:
        salt = _salt() if fingerprint else None
    kinds: dict[str, int] = {}

    def walk(node, key_name: str | None):
        if isinstance(node, dict):
            return {k: walk(v, k if isinstance(k, str) else None) for k, v in node.items()}
        if isinstance(node, (list, tuple)):
            return [walk(v, key_name) for v in node]
        if isinstance(node, str):
            if key_name:
                strong = bool(_KEY_NAME_STRONG.search(key_name))
                weak = bool(_KEY_NAME_WEAK.match(key_name))
                if (strong or weak) and not _looks_like_placeholder(node) \
                        and _value_is_secret_shaped(node, strong=strong):
                    kind = "password" if strong else "weak_keyed"
                    _bump(kinds, kind)
                    return _mark(kind, node, salt)
            r = redact(node, salt=salt)
            for k, n in r.kinds.items():
                kinds[k] = kinds.get(k, 0) + n
            return r.text
        return node

    return RedactResult(walk(obj, None), sum(kinds.values()), kinds)


# ---- 自己試験（アプリが起動時に「効いている」を確かめるため） -----------------
# ★画面に「機微は送りません」と固定文字列で書かない。ここを実際に通した結果を出す。
#   当たり/外れの両方を持つ。外れ側（誤爆）が無い自己試験は何も保証しない。
SELFTEST_MUST_REDACT = [
    "password=Password123!",
    "パスワードは Zx9-demo42 です",
    "-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNza\n-----END OPENSSH PRIVATE KEY-----",
    "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghij",
    "export ANTHROPIC_API_KEY=sk-ant-api03-Zm9vYmFyYmF6cXV4MTIzNA",
    "psql postgres://app:Tr0ub4dor@db.internal:5432/app",
    "sshpass -p 'Qm7vTx2k' ssh admin@10.0.0.5",
]
SELFTEST_MUST_KEEP = [
    "if password is None: raise ValueError('password required')",
    "git checkout 9ba44b0e6f2c1d4a8b3e5f7c9d1a2b4c6e8f0a12",
    "sha256: e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "model=Qwen3-Coder-30B-A3B-Instruct-Q4_K_M",
    "パスワードは変更してください",
    "PASSWORD=${DB_PASS}",
    "password: <YOUR_PASSWORD>",
    "uuid 550e8400-e29b-41d4-a716-446655440000",
]


def selftest() -> tuple[bool, list[str]]:
    """既知の当たり/外れを実際に通す。(合否, 不一致の一覧) を返す。"""
    bad: list[str] = []
    for s in SELFTEST_MUST_REDACT:
        if redact(s, salt=b"selftest").count == 0:
            bad.append("見逃し: " + s.replace("\n", " / ")[:60])
    for s in SELFTEST_MUST_KEEP:
        r = redact(s, salt=b"selftest")
        if r.count:
            bad.append("過剰: %s → %s" % (s[:40], r.text[:60]))
    return (not bad), bad


# ---- 濾し器としての入口（Rust / シェルから使う） ------------------------------
def _fix_stdio() -> None:
    """★cp932 の実機で死なないようにする（2026-09-02）。

    この経路は上の docstring が「アプリから直接掛けたいとき」の正規の入口として
    案内しているもので、実機（日本語 Windows）の Python は標準入出力を **cp932**
    で開く。JSONL には日本語も `‹伏字:…›` も入るので、

        echo '{"text":"パスワードは …"}' | python -m blackwall.redact --jsonl
        → UnicodeDecodeError: 'cp932' codec can't decode byte 0x81

    で**最初の1行で落ちる**。落ちた濾し器は「伏せなかった」のではなく
    「何も通さなかった」ので、呼ぶ側の作りによっては記録がまるごと消える。

    serve.py は同じ理由で `_fix_encodings()` を持っている（「実機で4回踏んでいる」）。
    こちらに無かったのは**片方で直して片方に残した**形。JSONL は定義上 UTF-8。
    """
    for name, errors in (("stdin", "replace"),
                         ("stdout", "backslashreplace"),
                         ("stderr", "backslashreplace")):
        s = getattr(sys, name, None)
        rec = getattr(s, "reconfigure", None)
        if rec is None:          # 差し替えられた io.StringIO 等（試験）
            continue
        try:
            rec(encoding="utf-8", errors=errors)
        except Exception:        # 付け替えられない場に居るなら、そのまま進む
            pass


def _main(argv: list[str]) -> int:
    _fix_stdio()
    if "--rules" in argv:
        json.dump([{"kind": r.kind, "why": r.why} for r in RULES],
                  sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    if "--selftest" in argv:
        ok, bad = selftest()
        json.dump({"ok": ok, "checked": len(SELFTEST_MUST_REDACT) + len(SELFTEST_MUST_KEEP),
                   "failures": bad}, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0 if ok else 1
    if "--scan" in argv:
        # 誤爆を測るための入口。落とした箇所を印つきで数えて出す（本文は出さない）。
        total, kinds, files = 0, {}, 0
        for path in argv[argv.index("--scan") + 1:]:
            try:
                body = Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files += 1
            r = redact(body, salt=b"scan")
            total += r.count
            for k, n in r.kinds.items():
                kinds[k] = kinds.get(k, 0) + n
            if r.count:
                for line in r.text.splitlines():
                    if MARK_OPEN in line:
                        sys.stderr.write("%s: %s\n" % (path, line.strip()[:160]))
        json.dump({"files": files, "count": total, "kinds": kinds},
                  sys.stdout, ensure_ascii=False)
        sys.stdout.write("\n")
        return 0

    jsonl = "--jsonl" in argv
    total, kinds, lines = 0, {}, 0
    inp = sys.stdin
    out = sys.stdout
    if jsonl:
        for raw in inp:
            lines += 1
            raw = raw.rstrip("\n")
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw)
            except json.JSONDecodeError:
                # ★壊れた行を黙って捨てない。伏字だけ掛けて、そのまま通す。
                r = redact(raw)
                out.write(r.text + "\n")
                total += r.count
                for k, n in r.kinds.items():
                    kinds[k] = kinds.get(k, 0) + n
                continue
            r = redact_obj(obj)
            out.write(json.dumps(r.text, ensure_ascii=False, default=str) + "\n")
            total += r.count
            for k, n in r.kinds.items():
                kinds[k] = kinds.get(k, 0) + n
    else:
        body = inp.read()
        lines = body.count("\n")
        r = redact(body)
        out.write(r.text)
        total, kinds = r.count, r.kinds
    out.flush()
    # 集計は**標準エラー**へ。標準出力は結果専用（serve.py と同じ規律）。
    sys.stderr.write(json.dumps({"count": total, "kinds": kinds, "lines": lines},
                                ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(_main(sys.argv[1:]))
