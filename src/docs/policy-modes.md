# 実行ポリシーの4モード（`laps/policy.py`）

`check_command()` / `check_path()` が返す `Verdict.allowed` を決める、唯一の実行許可
チョークポイント。system prompt に書いた注意書きは境界にならない（モデルは指示と
非信頼データを同じチャンネルで見る）という前提のもと、**モデルの外側**でこの1ファイルが
判定する。

## ⚠ まず区別: これは「実行モード」であって「倫理の刻み」ではない

このプロジェクトには**2本の別々の軸**があり、混同すると読み違える。

| 軸 | 値 | 環境変数 | 何を変えるか |
|---|---|---|---|
| **実行モード**（本書の主題） | `readonly` / `workspace` / `off` / `trusted` | `LAPS_POLICY` | **実行できる/できないの境界そのもの**（choke point） |
| 倫理の刻み | `strict` / `standard` / `direct` / `raw`（+測定専用 `sham`） | `LAPS_ETHICS` | モデルがどれだけ慎重に振る舞うか（確認を待つか等）。**policy の許可判定には一切影響しない**——`ethics.py` 自身が「raw にしても policy ゲートは1mmも緩まない」と明記している |

`raw` は**倫理の刻みの値**であって実行モードの値になったことがない
（`policy._mode()` が受理するのは `readonly`/`workspace`/`off`/`trusted` の4つだけ、
それ以外は `readonly` に安全側で落ちる）。「trusted / workspace / raw」のように
1本の並びとして書くと、この2軸を暗黙に混ぜてしまうので避けること。

## 4モード比較

既定は **`workspace`**（公開版。もとのラボでは `trusted` で使っていた）。未設定時と、認識できない値を渡したときの
扱いが違う点に注意——未設定は `workspace` に、`LAPS_POLICY=全部ゆるして` のような未知の値は安全側
`readonly` に、それぞれ倒れる（`_mode()`）。

| 観点 | `readonly` | `workspace` | `off` | `trusted` |
|---|---|---|---|---|
| 既定許可 | 読み取り専用プログラム(`_READ_ONLY`)のみ | 上記 + `_WORKSPACE_EXTRA`(mkdir/cp/mv/rm/tar/…・既定でインタプリタも含む) | 許可リストなし（何でも通る） | 許可リストなし（何でも通る） |
| `_ALWAYS_DENIED`（sudo/ssh/curl/apt/dd/mount/…） | 拒否 | 拒否 | **拒否**（off でも通らない床） | **通る** |
| 書き込み封じ込め（workspace 配下のみ） | 書き込みそのものを拒否 | workspace 配下のみ許可 | 封じ込めなし | 封じ込めなし |
| リダイレクト (`>`/`>>`) | 拒否 | workspace 配下のみ | 制約なし | 制約なし |
| `cd` の行き先が解決不能（`cd -`・変数展開・引数なし） | fail-closed で拒否 | fail-closed で拒否 | 素通し | 素通し |
| コマンド置換 `$(...)` / `` ` `` | 拒否（"解析できないため不可"） | 拒否 | trusted と同じ扱い判定には来ない※ | **通す**（2026-08-30～。実測4走行16回のブロックを解消） |
| 引用符/改行が壊れて `shlex` が例外 | fail-closed で拒否 | fail-closed で拒否 | trusted と同じ扱い判定には来ない※ | **通す**（"構文は解析できないがそのまま通す"。実測 43/15,645 件がこれで拒否されていた） |
| ssh | 許可リスト制（`LAPS_SSH_HOSTS` 空なら全拒否） | 許可リスト制 | 許可リスト制（**off はここが trusted と違う**——「手元の破壊は許すが外へ出る経路は拒む」という別の約束） | 接続先を縛らない。ProxyCommand/LocalCommand の検査だけは維持 |
| 自己保護（policy.py・permission.py・settings.json・audit.jsonl・postedit.json・hooks/・plugins/・PROMPTS/） | 書き込み拒否（読みは許可） | 同左 | **同左**（2026-09-02 まで off/trusted は自己保護ごと素通りしていた実装バグがあった。現在は `check_command`/`check_path` とも `_self_protect_verdict` に一本化） | 同左 |
| `authorized_keys`/`.ssh/`/`sudoers`（どこにあっても） | 拒否 | 拒否 | 拒否 | 拒否 |

※ `$(...)`/構文エラーの分岐は `mode != "trusted"` で readonly/workspace/off をまとめて
「拒否」側に倒しており、コード上 off はこの2点で readonly/workspace と同じ扱い
（`check_command` 冒頭、L584 と L596 を参照）。

## 「境界」として売ってはいけない、と policy.py 自身が書いている点

モジュール docstring（L37-48）が実測つきで明記している内容をそのまま引く:

- **readonly は「読むだけ」と言えない**: `sort -o /外/x /etc/hostname` や
  `awk 'BEGIN{print > "/外/x"}'` は許可リストのプログラムが持つ機能で、外にファイルが
  実際に作れる（プログラムごとに違うので列挙が閉じない）。
- **workspace は「作業フォルダの中だけ」と言えない**: `python3 evil.py` /
  `pytest`(conftest.py 経由) / `tar xPf` は外へ書ける。インタプリタは試験を走らせるために
  意図的に開けてある（`_WORKSPACE_EXTRA`）ので閉じられない。
- 閉じているのは「全プログラムに一度に効く」種類の穴だけ（`cd` の cwd 追従、
  `LD_PRELOAD=` 等の前置き）。本当の直し方は別 uid・bwrap・コンテナで、それは
  `policy.py` の外にある、と明記されている。

## 見つけた食い違い（今回の監査・報告のみ、コード未変更）

### `LAPS_POLICY` の既定解釈が実装によって割れている

`policy._mode()` / `envpack.py` / `gateway.py` は未設定時を **`trusted`** として扱う。
ところが `laps/tools/bash.py:717` は「いま trusted か」を

```python
os.environ.get("LAPS_POLICY", "").strip().lower() == "trusted"
```

と**別実装**しており、既定 `""` を使っている。未設定（実際の既定は workspace）のとき
`"" == "trusted"` は `False` になるため、**出荷既定の構成では、権限で拒否された直後に
出すはずの「trusted なら sudo が使えます」というヒント文が出ない**。`check_command`
自体の許可判定（sudo を実際に通すかどうか）はこの再実装と無関係に正しく動くので、
実行可否のセキュリティホールではなく、**ヒント文が出るべき場面で出ない UX 上の劣化**。
`tools/bash.py` は本担当（W5）の対象外のため、`laps/policy.py` の `_mode()` を
import して使うよう直すのは別担当への申し送りとする。

### heredoc の本文行が「別のコマンド」として個別に検査される

`_split_commands()` は改行を `;` と同じ区切りとして扱う設計
（複数行の `python3 -c "…"` インジェクションを1コマンドとして見逃さないための意図的な
選択、と docstring に明記）。この設計の副作用として、**heredoc の本文もコマンドとして
1行ずつ独立に検査される**——ヒアドキュメントという構文をこのゲートは特に知らない。

実際に `check_command()` を叩いて確認した挙動（`LAPS_POLICY` を切り替え、
`cwd` は一貫してテスト用ディレクトリ）:

```text
cat <<EOF
hello world
EOF
```

は `readonly`/`workspace` で **`` `hello` は許可リストに無い（mode=workspace） ``**
として拒否される（本文の1語目 `hello` を、あたかも実行するプログラム名であるかのように
検査してしまうため）。`cat` 自体は無害な読み取りだが、拒否理由の文言はこの原因を表していない。

`trusted`/`off` では本文行も1個の独立コマンドとして `_self_protect_verdict` にかけられる
ため、**本文の中身次第で挙動が変わる**。例えば本文に `rm -rf /` という行があると、
（heredoc として書いただけで実行の意図が無くても）自己保護判定に**偶然**引っかかって
拒否されることがある一方、無害な文面ならそのまま通る。fail-closed 側に倒れる分だけ
実害は無いが、**「なぜ拒否されたか」の理由が実態と一致しない**——ゲートを信頼して
理由文だけを読むと誤診する。

この振る舞いは今回の監査で発見したもので、意図的な設計か見落としかは `policy.py` の
コメントからは判断できない。**挙動そのものは変更せず**、次項の試験で固定した
（今後知らずに壊さないため）。ヒアドキュメントを安全に通したいなら、シェル文字列としてで
はなく Write ツールでファイルに書く運用を勧めるのが妥当——というのが `check_command`
の他の拒否理由が一貫して勧めている代替と同じ形。

## 引用符・改行・コマンド置換・heredoc・`;`/`&&` 連結の試験

上記の挙動は `tests/test_policy_quoting_and_control_flow.py` に固定した
（trusted/workspace 双方、あるいは4モード全部で検証するケースを分けている）。
新規に見つけた動作 3 点（`;`/`&&` 混在時の自己保護判定・heredoc 本文の独立検査・
`$(...)` を含む行の trusted 通過）を含む。個別の "trusted は解析不能構文を通す"
既存試験は `tests/test_policy_trusted_parse.py` に既にあり、重複させていない。
