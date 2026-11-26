# 環境変数スイッチ一覧（`LAPS_*`）

> ★名前は 2026-11-14 に `BLACKWALL_*` から `LAPS_*` へ変えた（正式名称 L.A.P.S）。**旧い `BLACKWALL_X` を設定している環境も今までどおり動く**:
> `laps` は import のいちばん最初に新旧の名前を写し合う（`laps/_envcompat.py`）。両方あれば `LAPS_X` が勝つ。空文字もそのまま写る。
> アプリ（Rust）は読むときに `LAPS_X` を先に、無ければ `BLACKWALL_X` を見る（`app/src-tauri/src/envcompat.rs`）。

`laps/` 以下で実際に読まれている `LAPS_*` 環境変数を機械的に列挙した表。**この表と `tests/test_env_switches_documented.py` はセットで機能する**——コードに新しいスイッチが増えたのにこの表に行が無い（逆に、行だけ残ってコードから消えた）とき、その試験が落ちる。

- 総数: **147**（`tests/_env_scan.py` の AST 走査で 126 個を自動発見 + 手動 1 個。後者の理由は `tests/_env_scan.py` の docstring 参照）。

  ⚠ **この表の `file:line` 列を機械で確かめる試験は無い**（`test_env_switches_documented.py`
  が突き合わせるのは**名前の集合だけ**）。そのため行番号は誰かがコードを 1 行足すたびに
  静かに古くなる——実際にずれていたことがある。
  **他領域の統合（docs-env 担当・critic の統合順序で最後に実施予定）で総数も file:line も
  再生成すること。**
- 総数: 表の行数がそのまま（`tests/_env_scan.py` の走査と `tests/test_env_switches_documented.py` が突き合わせる。数をここに書くと古くなるので書かない）。
- 「既定」列はコード上の `.get(NAME, default)` に書かれている値。空文字既定と「未設定」は意味が違う場合があるため、紛らわしい所は注記した。
- **対象外**: `os.environ` 経由でも `LAPS_` で始まらない標準変数（`PATH` / `PATHEXT` / `COMSPEC` / `SystemRoot` / `XDG_CONFIG_HOME`）はここに載せない——スイッチではなく OS の実行環境の読み取りのため。
- **スキャナが辿れる間接参照は3段**（`tests/_env_scan.py` docstring 参照）: ①直接の `os.environ.get/[]` ②「引数で env 名を受け取って読むだけの小さな関数」呼び出し ③`for var, key in DICT.items(): mapping.get(var)` というdict 丸ごとの走査（`LAPS_MAX_ITERS`/`TELEMETRY`/`PROSE_WIDTH` はこれで発見——最初のスキャナ版では見えておらず、後から拡張した）。**既知の盲点はただ1つ**: 辞書リテラル+タプル代入で1個ずつ拾う形（`LAPS_TESTVM2_PW`）は③の走査条件（同じ変数が丸ごと `.items()` される）に合わないため見つからない。手動リストで補っている——新しい間接参照を追加するときはこの手動リストかスキャナ自体を更新すること。

## 実行ポリシー・作業域（policy.py / envpack.py）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_BASH_BINARY_GUARD` | 1 | Bash の出力の先頭 4KB に NUL があれば「バイナリ」と告げて文字にしない（`0` で従来どおり decode）。Read の関門と同じ考え（2026-09-08 監査）。 | `laps/engine/tools/shell.py:532` | 1 |
| `LAPS_HOME` | ~/.laps（旧い ~/.blackwall が在ればそのまま使う・laps/paths.py） | エージェントの状態（セッション・監査ログ・PROMPTS 既定位置・checkpoints 既定位置・TodoWrite/Task の帳簿）を置く根。**自己保護の対象パスもここ基準**（`policy._self_home()`）。試験は必ず `tmp_path` へ退避させること（`tests/conftest.py` の `_isolate_blackwall_home` を参照）。 | `laps/engine/tools/misc.py:152` | 15 |
| `LAPS_HOOKS` | 1 | 利用者のフック（`<LAPS の家>/hooks.json`・Claude Code の hooks と同じ書き方: PreToolUse・PostToolUse・UserPromptSubmit・Stop）。0 で全部止める（2026-11-19） | `laps/engine/hooks.py:49` | 1 |
| `LAPS_NO_INTERPRETERS` | (未設定) | `1` で `python3`/`python`/`node`/`pytest`/`make` を workspace の許可リストから外す （既定では workspace でも実行できる——インタプリタは中身をゲートが見られないので workspace は「境界」ではないという弱点そのもの）。 | `laps/policy.py:131` | 1 |
| `LAPS_POLICY` | workspace | 実行モード。`readonly` / `workspace` / `off` / `trusted` のいずれか以外は `readonly` に落ちる（`policy._mode()`）。★注意: `laps/envpack.py:150` は 同じ判定を `os.environ.get("LAPS_POLICY", "").strip().lower() == "trusted"` と **別実装**しており、既定 `""` を使っているため**未設定時（実際の既定は trusted）に trusted と判定できない**——sudo のヒント文が出るべき場面で出ない。詳細は `docs/policy-modes.md` の「食い違い」節。 | `laps/engine/server.py:212` | 5 |
| `LAPS_PROMPT_DIR` | <src>/PROMPTS | system prompt（guardrails.md 等）を読む場所。自己保護の対象木にも入る。 | `laps/ethics.py:67` | 3 |
| `LAPS_SSH_HOSTS` | (空) | readonly/workspace/off で ssh の接続先を許可するホスト名のカンマ区切りリスト。空なら trusted 以外は ssh 全拒否。trusted は無視（接続先を縛らない）。 | `laps/policy.py:520` | 1 |
| `LAPS_SECRET_TREES` | (空) | 機微な物を置くフォルダ（`:` 区切り）。ここの中を再帰で読む命令と、ここを作業場所にする命令を readonly/workspace で断る。 | `laps/policy.py:219` |
| `LAPS_TASK` | 1 | 子のエージェントの道具 Task を渡す（engine/tools/task.py）。0 で渡さない（2026-11-19 旧い oc/delegate を外して書き直した） | `laps/engine/tools/__init__.py:214` | 1 |
| `LAPS_WORKSPACE` | os.getcwd() | `workspace` モードで書き込みを許す根ディレクトリ。gateway/serve が spec ごとに上書きする。 | `laps/engine/server.py:124` | 12 |

## 倫理の刻み（ethics.py）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_ETHICS` | raw | 慎重さの刻み: strict/standard/direct/raw（+測定専用 sham）。`policy.py` の許可判定には**一切影響しない**（実行可否はモデルの外側＝policy が決める設計）。不正な値は既定へ。 | `laps/ethics.py:62` | 2 |
| `LAPS_ETHICS_PAD` | 0 | 測定用の詰め物を有効化（`1`）。出荷既定の system prompt を変えないための、腕の長さを揃える目的の試験専用スイッチ。 | `laps/ethics.py:143` | 1 |
| `LAPS_HONESTY_PINS` | 1 | 「捏造しない/file:line で示す」等の正直さの固定文を system prompt に含めるか。`0` で外せるが、外すと測定値は信用できないと ethics.py 自身が明記。 | `laps/ethics.py:94` | 2 |

## バックエンド接続・サンプリング（backend.py）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_API_KEY` | sk-no-key-required | バックエンドへ渡す API キー（ローカル推論器は通常未使用）。 | `laps/backend.py:987` | 1 |
| `LAPS_BACKEND` | http://127.0.0.1:8080/v1 (DEFAULT_BASE_URL) | OpenAI 互換バックエンドの base URL（llama-server 等）。 | `laps/backend.py:386` | 9 |
| `LAPS_MODEL` | blackwall-coder (DEFAULT_MODEL) | バックエンドへ渡すモデル名。 | `laps/backend.py:988` | 7 |
| `LAPS_N_CTX` | (未設定→ /props から取得、失敗時 16384=FALLBACK_N_CTX) | コンテキスト長。`/props` で実測できないときの手動指定・上書き。 | `laps/backend.py:980` | 6 |
| `LAPS_OUT_CAP_FRACTION` | 0.25 | 1 回の道具の結果に許す量（**窓に対する割合**・トークン基準）。既定 0.25 は「結果 4 本で窓が埋まる」意味で、大きいファイルを数本読むと**圧縮が構造的に避けられない**。圧縮はプロンプト前方を書き換えるので接頭辞キャッシュが無効になり（この木の実測: 末尾追記 0.10s 対 先頭書き換え 26.00s＝**260倍**）、圧縮回数がそのまま体感の遅さになる。0.01〜0.90 の外や読めない値は既定に戻る。 | `laps/tools/limits.py:50` | 0.25 |
| `LAPS_PROFILE` | `local-coder` | 推論機の**銘柄**を名前で選ぶ（`laps/models.py` の表）。★1 つ選ぶと **base_url・モデル名・窓・道具の呼び方・調整値の方言**がまとめて決まる。以前は環境変数 4 つを整合させて渡す必要があり、1 つ忘れると黙って混ざった（32K のつもりで 256K を使う等）。使える名前は `local-coder`（既定）／`kimi-k2`／`kimi-k2-turbo`。 | `laps/backend.py:562` | `local-coder` |
| `LAPS_ALLOW_EXTERNAL` | （未設定＝外部を選べない） | `1` にすると**ラボの外へ出る銘柄**を選べる。★fail-closed にしてある理由: エージェントは作業場のファイルを読んで**そのまま文脈に載せる**ので、外部 API を選ぶことは**その中身を事業者へ送ること**と同じ。禁止ではなく「**知らずに使わない**」ための門。外部銘柄は併せて鍵（例 `MOONSHOT_API_KEY`）も要る。 | `laps/models.py:112` | 未設定 |
| `LAPS_FOLD_PROSE` | **`0`（畳まない）** | `1` にすると最深段（段8）で**モデル自身の発言**を1行目まで畳む（2026-09-17 以前の既定）。★切った根拠＝実機 8 セッションで窓の中身を測った結果 **道具の結果 95.4% ／ 発言 3.6% ／ 依頼 0.9%**（中央値・範囲 90.9〜97.7%）。**畳んでも最大 3.6% しか浮かない**のに、その 3.6% は**意図（なぜ・次に何を）の唯一の写し**で、ハーネスは観測できない。外の測定も同じ向き（SWE-agent ACI arXiv:2405.15793 は「畳むのは観測」・ACE arXiv:2510.04618 は「書き直すと context collapse」・arXiv:2605.12978 は「生の記録が 2 倍」）。自ラボでは発火した走行で**同じ段落を 5 回**繰り返した。⚠ **切った効果はまだ測っていない**（浮かない 3.6% のぶん仕切り直しが増える可能性）。A/B のために `1` を残してある。 | `laps/context_budget.py:962` | `0` |
| `LAPS_NO_STARTUP_PROBE` | （未設定＝確かめる） | `1` にすると、端末版（TUI）が**起動時に送り先へ聞きに行く確認をやめる**。既定では、対話で起動したときだけ `/health` と `/v1/models` を 1 回ずつ・**1 秒で打ち切って**聞き、①届かない ②見出しのモデル名を送り先が出していない、のどちらかのときだけ 1〜2 行で知らせる（問題が無ければ何も出さない）。llama-server は知らないモデル名でも黙って応えるので、設定が古いと見出しだけが嘘をつく——それを送る前に言うための確認。試験は `tests/conftest.py` が既定で `1` にしている（網を叩かない）。 | `laps/term/app.py:934` | 未設定 |
| `LAPS_RECONNECT_SECS` | `90` | **走行の途中で**推論機に繋がらなくなったとき、戻ってくるのを待つ秒数（2 秒おきに `/health` を見て、戻ったら送り直す）。`0` で待たない（従来どおり即失敗）。★待つのは**一度でも応答を受けた後だけ**＝1 通目の接続失敗は今までどおりすぐ理由を言って失敗する。推論機の再起動（モデルの切替・置き場の移動）は 21GB の読み直しで 15〜26 秒かかり、旧形ではその間の 1 回の接続拒否で数時間の走行が終わっていた。待っている間は `on_wait` で知らせる。中断されたら即座に抜ける。 | `laps/backend.py:1156` | `90` |
| `LAPS_ENV_SCRUB` | （未設定＝外す） | `0` にすると、Bash 道具が走らせる命令へ、名前に `KEY`・`SECRET`・`TOKEN` を含む環境変数もそのまま渡す（従来の挙動）。★既定で外す理由: `env` 1 回で鍵（例: 中継の `LAPS_GATEWAY_TOKEN`）が会話の記録に載り、要約や記憶を通って外へ出うる。手本は Codex の `protocol/src/shell_environment.rs` の既定の除外（2026-11-11 オーナー了承）。 | `laps/engine/tools/shell.py:79` | 1 |
| `LAPS_APPLY_PATCH` | 1 | ApplyPatch 道具（`engine/tools/patch.py`・Codex の apply_patch と同じ形の差分で複数のファイルを 1 回で変える・全部当ててから書く）を載せるか。`0` で外す（Bash の `apply_patch <<'EOF'` の読み替えは残る）。2026-11-11 オーナー了承。 | `laps/engine/tools/patch.py:326` | 1 |
| `LAPS_ASK_KEEP_PREFIX` | 1 | Ask の依頼を Agent の前置きと道具の一覧で走らせる（Ask で始めた新しい会話も・2026-11-14。前置きはアプリの指示の違いでは作り直さない）（`engine/server.py` の `_ask_keeps_prefix`）。書く道具は実行のときに断り、依頼の後ろに Ask の一言を足す。`0` で従来どおり（Ask 用の短い前置きと 9 本の道具に替える）。★既定で有効な理由: 前置きと道具の一覧が替わると推論機の控えが 1 文字目から使えず、切り替えのたびに会話を頭から読み直す（2026-11-13 の記録: Agent→Ask の 4 回すべて・53K〜154K トークン・138〜435 秒）。会話が Ask で始まっていて Agent の前置きがまだ無いときは、どちらの値でも Ask の前置き。 | `laps/engine/server.py:445` |
| `LAPS_SHELL_SANDBOX` | auto | Bash 道具の囲い（bubblewrap・`engine/tools/shellbox.py`）。`auto`＝開発モードだけ囲う／`on`＝どのモードでも／`off`＝囲わない。囲いの中は根が読むだけで、作業場・`/tmp`・`/var/tmp`・Blackwall の置き場・家の道具の置き場（`.cache` `.npm` `.local` `.cargo` など）だけ書ける。作業場の `.git` は読むだけ。モデルは `Bash(sandbox=false, reason=…)` で外を選べる（関所 policy.py はそのまま効く）。Linux で bwrap が動くときだけ。手本は Codex の linux-sandbox（2026-11-11 オーナー「開発の作業だけ囲う」で了承）。 | `laps/engine/tools/shellbox.py:50` | 1 |
| `LAPS_UNDO` | （未設定＝控える） | `0` にすると、依頼ごとの取り消しの控え（`engine/undo.py`・会話の置き場の `undo/<依頼>/`）を取らない。控えがあると、依頼の終わりに「変えたファイル（+行 −行）」を出し、`/undo` で直前の依頼の変更を戻せる（依頼の後に変わったファイルは上書きしない・Bash の変更は戻らない）。手本は Codex の turn_diff_tracker と旧い中核の checkpoint.py（2026-11-11）。 | `laps/engine/undo.py:226` | 1 |
| `LAPS_KEEP_ENV` | （空） | 外さずに残す環境変数の名前をコンマで並べる（例: `GH_TOKEN,HF_TOKEN`）。大文字小文字は区別しない。`LAPS_ENV_SCRUB` で外す物のうち、命令が本当に要る物だけを残すため。 | `laps/engine/tools/shell.py:81` | 1 |
| `LAPS_BASH_RC` | （未設定＝rc を読む） | `0` にすると Bash 道具が `~/.bashrc` を読まず alias も展開しない（従来の挙動）。★既定で読むようにした理由: 課題 `create-bucket` は Dockerfile の最終行で `alias aws="awslocal"` を `~/.bashrc` に書く**だけ**でコマンドを差し替えており、非対話 bash は **rc を読まない＋alias を展開しない**の二重で前提を失っていた（実機で確認・最小ループ側の同じ修正は 24 課題の 5 本を取り戻した）。rc の出力は捨て、失敗しても続け、対話ガードのある普通の rc は早期 return するので無害。 | `laps/engine/tools/shell.py:124` | 未設定 |
| `LAPS_READ_TIMEOUT` | 600 | バックエンド HTTP 応答待ちのタイムアウト秒。★2026-10-06 90→600（Qwen3.8 は一括の応答に数分かかる）。 | `laps/backend.py:275` | 2 |
| `LAPS_STUCK_GATE` | 0 | 引数を変えながら失敗し続ける形を、言葉の次の段で止める（2026-10-19 オーナー「2 で進めて。ClaudeCode の仕組みとかも調べて小さいモデル用に最適化してみて」）。同じ型の失敗の 6・9・12…回目か、直近 10 回のうち 8 回の失敗で、次の手番が本文を書かずに道具だけ呼んだら走らせず 3 行（目的・エラー・別の手）を書かせる（最大 2 手番）。9 回目で要約を前倒しして文脈を作り直す（1 依頼 1 回）。KAT-Coder の走行で同じ失敗が 7.9 回／100 手番（Qwen3.6 は 0.8）・課題集で npm test の変種を 337 回。`0` で切る（添え書きは残る）。 | `laps/engine/aids.py:418` | 1 |
| `LAPS_LOOP_GUARD` | 0 | 間を挟んだ繰り返しと往復を止める（2026-10-21 オーナー「様々なOSSのエージェントを調べて…」→「導入してください」・Qwen Code の loopDetectionService と OpenHands の stuck_detector に倣う）。同じ呼び出しが同じ結果を返した回数を 1 依頼の中で連続でなくても数え、6 回目は走らせない。A→B の往復を 3 回くり返し、どちらも毎回同じ結果なら最後の結果に一言を足し、続けて同じ組を呼んだら走らせない（別の呼び出しを挟めば外れる）。結果はハーネスの注記の段落・時刻・かかった秒を外して比べる（直してから試験を回し直すように結果が変わる繰り返しは数えない）。走行は止めない。`0` で切る。数え: `repeat_anywhere_blocked`・`alternate_noted`・`alternate_blocked`。★2026-11-07: git の様子見（status・ファイルを指さない diff・ls-files）だけを 6 回続けたら一言（Qwen Code の shell inspection stagnation・数え `git_look_noted`）。 | `laps/engine/aids.py:405` | 1 |
| `LAPS_INJECT_ESCALATE` | 30 | 走行中の普通の一言（Enter）を待たせる上限（秒）。過ぎても Bash・BashOutput 以外の道具が動いていたら止めて読む（engine/loop.py:0・2026-10-31 問いに 3 分 44 秒反応しなかった実例）。子の作業と問いの答え待ちは止めない。0 で切る | `laps/engine/loop.py:405` | 1 |
| `LAPS_POLL_BACKOFF` | 1 | 同じ背景の仕事（J1 や pid）を BashOutput で 2 回目に見に行くときから、最短の待ちを 30→60→120→300 秒と延ばす（2026-10-21・オーナーの画面で同じ思考が手番ごとに並んだ・BashOutput 22 回。Claude Code の TaskOutput は既定で待つ）。終われば待たずに戻る。`0` で切る。 | `laps/engine/tools/shell.py:873` | 1 |
| `LAPS_SELF_REVIEW` | 1 | 作業したモデル自身の敵対レビュー（oc/selfreview・2026-10-21 オーナー「子を使わずに…親モデルが自分で敵対レビューするように」）。この走行がファイルを変えたら終える前に 1 回、依頼の原文と作業フォルダの変更を添えて、新しく道具を走らせて壊しにいく見直しを頼む。道具を 1 つも走らせずに見直しを終えたら 1 回差し戻す。利用者の「レビューして」「見直して」にも同じ手順で応える。✓/✗ の数を画面に出す。終わりの照らし合わせ（criteria）はこれに含む。`0` で切る（試験の既定は 0）。 | `laps/oc/selfreview.py:30` | 1 |
| `LAPS_SELF_REVIEW_MIN_TOOLS` | 8 | 新しい中核で、終える前の見直し（`LAPS_SELF_REVIEW`）を頼むのは、ファイルを変え、道具をこの回数以上使った走行だけ（小さい依頼に 1 周足さない・2026-10-30）。利用者が「レビューして」と頼んだときはこの数と関係なく見直す。 | `laps/engine/finish.py:255` | 1 |
| `LAPS_CONSTRUCT_NIGHT` | 0 | ★既定は切る（2026-10-21 オーナー「1（既定で止める）・2 で試してみて」・要点が作業ログ・帳面・段 1 と重なり効き目は未測定）。`1` で入る。Construct の夜の整理（段 3b・serve が空いている時だけ）。夜の窓（この機械の 1〜5 時）で、走行も待ち行列も空で、前の整理から 20 時間たっていれば、新しい記録が 3 件以上増えたフォルダ（最大 3 つ）の「プロジェクトの要点」（.blackwall/PROJECT.md）を推論機に作り直させる。依頼が来たら接続を切って譲る。Claude の中継（API）には向けない（ローカルの推論機だけ）。手で動かす `blackwall construct consolidate` はいつでも使える。 | `laps/oc/construct.py:1362` | 1 |
| `LAPS_PARALLEL_HINT` | 1 | ★2026-11-20 前置きの「互いに関係の無い呼び出しは同じ手番でまとめる」を、例（Read を 3 本・Glob と Grep 2 本を同じ手番で）と損（手番が増えるたびに考え直しが 1 回）つきの強めた形にする。11-14〜20 の 53 走行で 1 手番あたり 1.1 本だった（旧の一般論の 1 行では効かなかった）。`0` で旧の 1 行。前置きが変わるので、続きの会話は次の 1 回だけ頭から読み直す。 | `laps/engine/prompt.py:65` | 1 |
| `LAPS_WAIT_DEFER` | 1 | ★2026-11-22 オーナー「JOB の完了まで待機する動作をバックグラウンド待機にして、その他の作業を先回りで進めておく動作にできる？」。BashOutput(wait≥30 秒) を、予定表に未着手（pending）が残っていて仕事がまだ動いていれば、1 つの仕事につき 1 回だけ待たずに返し、結果を使わない項目を先に進めるよう促す（結果が要るならもう一度呼べば待つ）。子・send・kill・終わった仕事・サーバーでは先送りしない。前置きの背景の仕事の 1 行も「待つ前に結果を使わない仕事を」に替える。11-10〜22 の 305 走行で BashOutput(wait) が道具の時間の 71%（12.3 時間）・うち 8.9 時間は未着手が残ったまま待っていた。`0` で切る（前置きも旧の 1 行）。前置きが変わるので、続きの会話は次の 1 回だけ頭から読み直す。数え: `wait_deferred`・`tools_while_deferred`・`wait_after_defer`。 | `laps/engine/prompt.py:78` | 1 |
| `LAPS_BASH_SNAPSHOT` | 1 | ★2026-11-20 壊す形の Bash の前に状態を控える（engine/tools/bashsafe.py）。rm・mv・sed -i・`>` の上書き・cp の上書き先（PowerShell の Remove-Item・Set-Content など）が触る既存のファイルを依頼の控えへ入れて /undo と Rewind で戻せるようにし（フォルダは 2000 個・256MB まで）、systemctl・*-Service の前後の状態を比べて戻し方を添え、ip・iptables・nft・ufw の前に網の状態を控えのフォルダへ書き出す。語の形だけで決める（取りこぼしはありうる）。`0` で止める。 | `laps/engine/tools/bashsafe.py:33` | 1 |
| `LAPS_CONSTRUCT_AUTO` | 1 | ★2026-11-20 依頼の始めに、強く当たる記憶（点数 0.45 以上・まだ文脈に無い物）を**断片だけ**最大 2 件添える（`[Memory]`・中身は出さない・開くのはモデルが Construct(open, why) で）。続きの依頼でも毎回・依頼の後ろに足すので前の文字列は変えない。外部の評価で「記憶を引かない（走行の 8%）」が最優先の指摘になったため。★10-21 のオーナーの判断（自動で中身を添えない）と違うのは、中身を渡さない所。見せた数 `construct_auto_shown`・開いた数 `construct_auto_opened`。`0` で止める。 | `laps/oc/construct.py:500` | 1 |
| `LAPS_CONSTRUCT` | 1 | Construct（手掛かりで思い出す記憶・oc/construct・2026-10-20 オーナー「導入してください。慎重に一つづつ正確に」「名前は Construct」）。~/.blackwall/construct.db に、出来事・分かったこと・行き止まり・効いた直し方・利用者の訂正・やり残しを「要旨 1 行＋場所＋出どころ」で持つ。★2026-10-21 から記憶の中身は自動で添えない（オーナー「自動で添える機能自体がだめ」・★11-20 から断片だけは LAPS_CONSTRUCT_AUTO で添える）: モデルが道具 Construct で思い出し（query で断片だけ・点数は手掛かりの当たり×ACT-R の活性×出どころ×フォルダ）、要る物を open と why（今の作業との関係）で開く。ハーネスに残すのは、利用者の訂正の止め・記録・呼ぶべき場面の数え（construct_shadow_*）。推論機は呼ばない。設計 DESIGN-memory-20261020.md。`0` で全部止める。 | `laps/engine/tools/__init__.py:215` | 1 |
| `LAPS_DONE_CRITERIA` | 1 | 何かを作る・直す・まとめる依頼で、①走行の初めに「できたと言える条件」を依頼の言葉で書き出させ（TodoWrite に 1 つずつ・元と同じを求める依頼は数で比べる項目も）②終える前に 1 回、結果を依頼と 1 つずつ照らし合わせて「✓ 条件 — 確かめ方」「✗ 条件 — 足りない物」を書かせる（ファイルを書き換え・道具を 8 回以上使った走行だけ・答えに一覧が既にあれば聞かない）。2026-10-19 オーナー了承（開発中のゲームの走行で、動いた時点で満足し、求められた構造と照らさずに終えた）・10-20「進めていいです」。`0` で両方を切る。 | `laps/engine/aids.py:177` | 1 |
| `LAPS_SYSCHANGE_GATE` | 1 | 作業フォルダの外のシステムの設定（/etc への書き込み・systemctl の再起動・resolvectl・ufw・ip route・apt install など・ssh で送る物も）を変える呼び出しが、本文の無い手番に来たときの扱い（oc/disclose）。既定（1）は★ハーネスが何を変えるかを 1 行知らせて、そのまま走らせる（2026-10-30 オーナー「進めていいです」・旧は走らせずに言わせた＝毎回 1 手番を使った: Sonnet の TB で 8 本中 5 本）。`block` で旧の形（1 走行 1 回止めて言わせる・2026-10-20 SNS の撮影の走行で本文が空のまま DNS の設定を書き換えたのがきっかけ）。`0` で知らせもしない（最後の答えに付ける「この作業で変えたシステムの設定」の一覧は切らない）。 | `laps/engine/aids.py:303` | 1 |
| `LAPS_PROGRESS` | 1 | 道具の呼び出しに `progress`（利用者への一文: 何が分かり、次に何をするか）の欄を足し、画面に出す（道具には渡さない）。2026-10-30 オーナー「内部思考を出さないようにしてモデルに対して、タスク状況を報告しながら進めるように」。本文で頼んでも小さいモデルは書かない（道具の手番に一言を添えた割合: Opus 12〜21%・Sonnet 14%・KAT 1〜4%・North 0%）。`0` で欄を足さない（端末版は思考の全文を出す）。 | `laps/oc/localguide.py:401` | 1 |
| `LAPS_STREAM_FIRST_BYTE` | 360（0 で無効） | 逐次配信で最初の1バイトが来るまでの上限秒（＝読み込み時間）。超えたら stream を切り、生存確認して非ストリーミングで1回だけ取りに行く。★2026-10-06 45→360（Qwen3.8 は窓 64K の読み直しに約 300 秒）。 | `laps/backend.py:321` | 1 |
| `LAPS_HTTP_TRUST_ENV` | 1（未設定=proxy を尊重） | 推論機への httpx が OS の HTTP(S)_PROXY/NO_PROXY を読むか。既定は読む（proxy 経由でしか届かない機械があるため）。0 で proxy を無視して直結（proxy が LAN を横取りする環境向け）。 | `laps/backend.py:301` | 2 |
| `LAPS_BROWSER` | （空＝Edge/Chrome を探す） | Serve 道具の撮影（screenshot=true）に使う headless のブラウザの実行ファイル（2026-10-10）。 | `laps/oc/servetool.py:134` | 1 |
| `LAPS_GATEWAY_RUN_MAX_SECS` | 43200 | 画面の接続が切れても走行中の会話を生かしておく上限（秒）。走行が終わってから猶予（600 秒）を数える（2026-10-10）。 | `laps/gateway.py:576` | 1 |
| `LAPS_GREP_SECS` | 60 | rg が無い機械で Grep が探す時間の上限（秒）。超えたらそこまでの結果と打ち切った旨を返す（2026-10-10）。 | `laps/engine/tools/files.py:714` | 1 |
| `LAPS_COMPACT_CONTINUE` | `1` | `0` で、要約を会話の続きとして頼むのをやめ、会話を文字にまとめ直す従来の形にする（毎回ゼロから読む）。2026-11-03。 | engine/compact.py:0 |
| `LAPS_COMPACT_AT` | （空＝Gemma は 64000・GLM は 30000・それ以外は窓−出力の上限） | 要約を始める文脈の語数（8192 以上）。Gemma は 2026-10-13 に 45000 → 64000（要約のたびに事実と手引きが落ちるのを減らす・P40 の Gemma は長い文脈でも生成の落ちが 15% ほど）。 | `laps/engine/compact.py:69` | 1 |
| `LAPS_IMAGE_HISTORY` | 3 | 会話に画像のまま残す枚数の基準。古い画像は文字（パスの一行）に替えるが、★読み込みの控えが切れないよう 3 枚ごとにまとめて決める。★2026-11-16〜 その画像から後ろが長い（推定 1.2 万トークン超）物は替えずに画像のまま残す（替えるとそこから後ろを全部読み直すため）・要約の後にまとめて替える。決めた事は記録に残し変えない。 | `laps/engine/loop.py:524` | 1 |
| `LAPS_FREEZE_TOOLS` | 1 | `0` で、道具の一覧を会話ごとに保存して使い回すのをやめる。★2026-11-16〜 道具の形（名前・引数・型・選択肢・順番）が同じなら、前に送った説明のまま送る（更新で説明が変わると、長い会話を頭から読み直していた）。形が変わったときと要約の後は今の物にする。 | `laps/engine/loop.py:583` | 1 |
| `LAPS_SLOT_CACHE` | 1 | `0` で、推論機の席の控えをやめる。★2026-11-16〜 Strata のとき、子のエージェントを起こす前に親の席を推論機の置き場へ保存して終わったら戻す・別の会話へ移るときに前の会話の席を保存して次の会話の保存を戻す（読み直すと 20 万トークンで約 7 分・保存と戻しは 3〜15 秒）。2 万トークン未満は保存しない。古いファイルは 推論機 の strata-session-gc が片付ける。 | `laps/engine/slots.py:53` | 1 |
| `LAPS_MODEL_FAMILY` | （空＝推論機の /props の model_path から判定） | 推論機に載っているモデルの系統を決め打ちする（`gemma`・`qwen`・`glm`・`cohere`）。`gemma` ならサンプリングを Gemma 4 の推奨（temperature 1.0・top_p 0.95・top_k 64）にする（2026-10-10）。 | `laps/backend.py:413` | 1 |
| `LAPS_MAX_OUTPUT_TOKENS` | （未設定＝窓の 1/4・4096〜32000） | oc 中核の 1 回の生成の上限（opencode の maxOutputTokens）。溢れの判定は「使ったトークン ≥ 窓 − この値」。 | `laps/engine/llm.py:271` | 1 |
| `LAPS_DISABLE_PROJECT_INSTRUCTIONS` | （未設定＝読む） | `1` で oc 中核が作業場の AGENTS.md / CLAUDE.md を前置きに読まない（opencode の OPENCODE_DISABLE_PROJECT_CONFIG に当たる）。 | `laps/engine/prompt.py:137` | 1 |
| `LAPS_COMPACT_EARLY` | 0（切） | ★2026-11-27 既定を切った（100K 以上を読み込んでから要約し、また読み込むため時間がかかる）。量を渡すと使う（例 100000）。新しい依頼を受けた時に、会話の長さ（トークンの見積もり）がこの量を越えていたら、作業を始める前に 1 回だけ要約して最近の分（約 1.5 万）と要約に戻す（手元の推論機だけ）。作業の途中ではやらない。`0` で切る（窓の手前の要約だけに戻る）。16000 より小さい値は 16000。2026-11-25 オーナー「会話が続くとどんどん蓄積されていってしまうので Claude アプリみたいな感じの仕組みにしたい」。 | `laps/engine/compact.py:106` | 1 |
| `LAPS_COMPACT_LEAN` | `1` | `0` で、要約の後に付ける物を前の形に戻す（作業ログの塊・要約と同じ帳面の行・ほかの会話の帳面の行を付け、新しい依頼の頭の要約でも最近の分を約 1.5 万残す）。既定は、作業ログは場所だけ・帳面は要約に無いこの会話の行だけ（6 千字まで）・新しい依頼の頭では最近の分を約 6 千（compact.BOUNDARY_TAIL）。2026-11-26 オーナー「会話が長くなるとコンテキスト圧縮率が落ちる」。 | engine/compact.py:0 |
| `LAPS_SLOT_SAVE_AT_END` | 1 | `0` で、依頼の終わりの席の保存をやめる。★2026-11-25〜 大きい会話は依頼が終わった後に裏で席を保存しておき、別の機械の L.A.P.S が推論機を使った後でも、次の依頼でその控えから戻す（頭から読み直さない）。 | `laps/engine/slots.py:199` | 1 |
| `LAPS_SLOT_SAVE_MIN` | 40000 | 依頼の終わりに席を保存する会話の大きさ（トークン）。これより小さい会話は保存しない。 | `laps/engine/slots.py:201` | 1 |
| `LAPS_SLOT_SAVE_STEP` | 8000 | 前の保存からこのトークン数以上伸びたときだけ、依頼の終わりに保存し直す（毎回 GB 単位で書かない）。 | `laps/engine/slots.py:205` | 1 |
| `LAPS_DESKTOP` | （未設定＝載せる） | `0` で oc 中核の Desktop 道具（Windows の画面を UI Automation の木で読み、番号で押す・打つ）を棚から外す。Windows 以外と csc.exe の無い機械では最初から載らない。 | `laps/oc/desktop.py:93` | 1 |
| `LAPS_MODELCTL_URL` | （空＝自動） | この機械のゲートウェイが、Android の上のモデル選び（操作の線の `server.models`・`server.switch`）を頼む先。空なら宛先そのものの `/modelctl/`（Claude の中継）→ 宛先と同じ機械の `:8743`（推論機の モデル切り替えの口）の順に試す（Windows のアプリの modelctl.rs と同じ決め方）。2026-10-19 オーナー「Android 版アプリの UI/UX を Claude のスマホ版アプリに寄せてほしい」。 | `laps/remotectl.py:572` | 1 |
| `LAPS_REMOTE_DESKTOP` | （未設定＝受ける） | `0` で、この機械のゲートウェイが中継越しの画面操作（操作の線の `desktop`・Claude Code の MCP の desktop 道具が使う）を断る。2026-10-14 オーナー「B で進めて」（Claude のセッションから Blackwall のデスクトップ操作を使う）。ゲートウェイはアプリが起こす＝ログイン中の画面のセッションで動くので窓に届く（SSH の命令は Session 0 で届かない）。中継に合言葉が無い（LAPS_RELAY_TOKEN が空）間は LAN から誰でも呼べるので、要らない機械ではここで切る。 | `laps/remotectl.py:528` | 1 |
| `LAPS_LISTEN` | （未設定＝載せる） | `0` で oc 中核の Listen 道具（音声ファイルの台詞を whisper.cpp で文字にする）を棚から外す。Windows / Linux の x64 以外では最初から載らない。 | `laps/oc/listen.py:99` | 1 |
| `LAPS_LISTEN_DIR` | （未設定＝`~/.laps/listen`・旧い `~/.blackwall` が在ればその中・Windows のアプリは `<データ根>\listen` を渡す） | Listen が whisper.cpp の本体とモデル（初回に取得・sha256 照合）を置く場所。 | `laps/oc/listen.py:107` | 1 |
| `LAPS_LIVE` | 1 | `0` で、走行中の事象の写し（`sessions/<id>/live.ndjson`）と受け箱（`inbox.ndjson`）を作らない＝スマホの「追う画面」からその走行を見られず、割り込み・中止・答えも受けない（2026-10-10・画面のリンク）。 | `laps/engine/server.py:501` | 1 |
| `LAPS_DESKTOP_DIR` | （未設定＝`~/.laps/desktop`・旧い `~/.blackwall` が在ればその中・Windows のアプリは `<データ根>\desktop` を渡す） | Desktop の補助プログラム（csc で組み立てる）を置く場所。 | `laps/oc/desktop.py:112` | 1 |
| `LAPS_BROWSER_INDICATOR` | （未設定＝出す） | `0` で Browser の操作中の表示（ページの縁の光・カーソル・右下の「L.A.P.S が操作中」と止めるボタン）を出さない（2026-11-18）。 | `laps/oc/browsercdp.py:1035` | 1 |
| `LAPS_CHROME_ASK_SITES` | （未設定＝聞く） | `0` でふだんの Chrome（拡張機能の線）で初めてのサイトを開く前に許可を聞かない（2026-11-18）。 | `laps/oc/chromebridge.py:311` | 1 |
| `LAPS_CHROME_BRIDGE` | （未設定＝使う） | `0` で L.A.P.S の Chrome 拡張機能とつなぐ口を開かない（アプリの外の形・2026-11-18）。 | `laps/oc/chromebridge.py:266` | 1 |
| `LAPS_CHROME_BRIDGE_PORT` | （未設定＝設定ファイルの値・既定 18765） | 拡張機能とつなぐ口の番号（127.0.0.1 だけで待つ・2026-11-18）。 | `laps/oc/chromebridge.py:48` | 1 |
| `LAPS_DESKTOP_INDICATOR` | （未設定＝出す） | `0` で Desktop の操作中の表示（画面の縁の光・押す場所の輪・右下の札・Ctrl+Alt+Q で止める）を出さない（2026-11-18）。 | `laps/oc/desktop.py:572` | 1 |
| `LAPS_PREPARE_PARTS` | （未設定＝そろえる） | `0` で、起動時に裏で道具の部品（Listen・Desktop・ffmpeg）をそろえるのを止める（Windows のアプリと端末版）。 | `laps/term/client.py:165` | 1 |
| `LAPS_VOICE` | （未設定＝載せる） | `0` で oc 中核の Voice 道具（文を声にする・参照の声を写す・Chatterbox Multilingual）を棚から外す。Windows / Linux の x64 以外では最初から載らない。 | `laps/oc/voice.py:67` | 1 |
| `LAPS_VOICE_DIR` | （未設定＝`~/.laps/voice`・旧い `~/.blackwall` が在ればその中・Windows のアプリは `<データ根>\voice` を渡す） | Voice が uv・専用の Python 3.11・PyTorch・Chatterbox・モデル（合わせて 5〜7GB）と保存した声を置く場所。 | `laps/oc/voice.py:75` | 1 |
| `LAPS_VOICE_GPU` | 1 | `0` で NVIDIA の GPU があっても Voice を CPU 版の PyTorch で入れて動かす。 | `laps/oc/voice.py:80` | 1 |
| `LAPS_API` | （未設定＝載せる） | `0` で oc 中核の Api 道具（他のプラットフォームの API を呼ぶ・保存した API キーを `{{NAME}}` で差し込む）を棚から外す。 | `laps/oc/tools.py:2585` | 1 |
| `LAPS_APIKEY_VALUE` | （アプリが自動で入れる） | **アプリが出す側**のスイッチ。設定画面の API キーの保存で、値を引数に載せず `python -m laps.apikeys set` へ渡す。人が設定する物ではない。 | `laps/apikeys.py:252` | 1 |
| `LAPS_MCP` | （未設定＝使う） | `0` で MCP（`mcp.json` の サーバーの道具）を棚に載せない。 | `laps/oc/mcp.py:42` | 1 |
| `LAPS_MCP_PROJECT` | （未設定＝読まない） | `1` で作業場の `.mcp.json` も読む。既定で読まないのは、取ってきたリポジトリに書かれた命令を黙って走らせないため。 | `laps/oc/mcp.py:85` | 1 |
| `LAPS_MCP_MAX_DIRECT` | 12 | MCP の道具が合わせてこの本数以下なら `mcp__<server>__<tool>` として直接載せ、超えたら道具 1 本（`Mcp`: list / describe / call）にまとめる（道具が多いと選び方が崩れる）。 | `laps/oc/mcp.py:1158` | 1 |
| `LAPS_MCP_START_WAIT` | 30 | 控えの無いサーバーを、走行の開始でつなぎ終わるまで待つ秒数。間に合わないものは裏で起動を続け、次の依頼から載る。 | `laps/oc/mcp.py:751` | 1 |
| `LAPS_MCP_START_TIMEOUT` | 90 | サーバー 1 つの起動（initialize と tools/list）を待つ上限秒。 | `laps/oc/mcp.py:627` | 1 |
| `LAPS_MCP_TIMEOUT` | 180 | MCP の道具 1 回の呼び出しを待つ上限秒。 | `laps/oc/mcp.py:1010` | 1 |
| `LAPS_MCP_ROOTS` | （未設定＝名乗らない） | `1` で MCP の roots（作業場のフォルダ）を名乗る。既定で名乗らないのは、ファイル用のサーバーが設定のフォルダを捨てて作業場に置き換えるため（server-filesystem で実測）。 | `laps/oc/mcp.py:263` | 1 |
| `LAPS_MCP_INLINE_SNAPSHOT_KB` | 256 | MCP の結果の `### Snapshot` が作業場のファイル（Playwright MCP の `.playwright-mcp\page-*.yml`）を指していたら、その中身を結果の末尾に入れる（この KB を超えるファイルはリンクのまま）。`0` で止める。開くたびに Read する 1 手番を省くため。 | `laps/oc/mcp.py:818` | 1 |
| `LAPS_MCP_JSON` | （アプリが自動で入れる） | **アプリが出す側**のスイッチ。設定画面の MCP の保存で、`mcp.json` の中身を `python -m laps.oc.mcp set` へ渡す。人が設定する物ではない。 | `laps/oc/mcp.py:1302` | 1 |
| `LAPS_REA` | （未設定＝使う） | `0` で同梱の REA（rea-agents の MCP サーバー・逆解析）を MCP の棚に足さない。同梱の木が無ければ元から何も足さない。利用者の `mcp.json` に `rea` があれば（`disabled` でも）そちらが勝つ。走行の開始では起動せず、最初の `Mcp`（list で server=rea・describe・call）で node を起動する（2026-11-03）。 | `laps/oc/mcp.py:138` | 1 |
| `LAPS_REA_DIR` | （未設定＝`LAPS_INSTALL_PYRUNTIME` の `rea/` があればそこ、Linux の .deb は venv の隣の `/opt/blackwall/rea`、無ければ `python.exe` の隣の `rea/`＝`pyruntime/rea`） | 同梱の REA の置き場（非 ASCII の利用者名で複製した pyruntime ではなく、インストール先の `rea/` を優先する・2026-11-03）。`node/node.exe`（Windows 以外は `node/node`）と `pkg/node_modules/rea-agents/scripts/rea.mjs` が両方あるときだけ使う。 | `laps/oc/mcp.py:119` | 1 |
| `LAPS_REA_GHIDRA` | （未設定＝`GHIDRA_INSTALL_DIR`、無ければ `<LAPS_HOME>/tools/ghidra/ghidra_*_PUBLIC`） | Ghidra の置き場（Ghidra と Java は同梱しない）。見つかれば REA に `GHIDRA_INSTALL_DIR` と `REA_ANALYSIS_PROVIDER=ghidra` を渡す。Windows は `scripts/install_ghidra.ps1` が `%LOCALAPPDATA%\Blackwall\agent\tools` に入れる（2026-11-05）。無いと逆コンパイルの道具（binary_overview・list_procedures・procedure_pseudo_code）は provider_unavailable、静的な読み手（SQLite・IL2CPP・.NET・APK）は動く。 | `laps/oc/mcp.py:146` | 1 |
| `LAPS_REA_JAVA` | （未設定＝`<LAPS_HOME>/tools/jdk/jdk-21*`） | Ghidra に使う JDK 21 の置き場。見つかれば REA に `JAVA_HOME` を渡し、`bin` を PATH の前へ足す（JAVA_HOME が無いと Ghidra が provider_unavailable・2026-11-05）。Ghidra が見つからないときは見ない。 | `laps/oc/mcp.py:152` | 1 |
| `LAPS_DOCS` | （未設定＝載せる） | `0` で Doc 道具（Word・PowerPoint・Excel・PDF を作る・読む）を棚から外す。 | `laps/oc/doctool.py:31` | 1 |
| `LAPS_DOCS_DIR` | （未設定＝`~/.laps/docs`・旧い `~/.blackwall` が在ればその中） | 部品を同梱していない機械（Linux の .deb）で、Doc が初回に作る専用の環境（uv・Python 3.12・約 300MB）の置き場。Windows は同梱なので使わない。 | `laps/oc/doctool.py:39` | 1 |
| `LAPS_APP` | （未設定＝載せる） | `0` で App 道具（アプリ・ゲーム・エディタ・サーバーをバックグラウンドで起こして動かし続ける・list / log / stop）を棚から外す。 | `laps/oc/apptool.py:527` | 1 |
| `LAPS_UEMAP` | （未設定＝載せる） | `0` で UeMappings 道具（Unreal のゲームを UE4SS つきで一度起こして .usmap を作り、足した物を消す）を棚から外す。Windows で、作業場に Unreal のゲームらしい物があるときだけ載る。 | `laps/oc/uemap.py:370` | 1 |
| `LAPS_GUIDE` | （未設定＝小さい核のとき local） | 小さいモデル向けの手引き（oc/localguide・2026-10-18 オーナー「モデルがエージェントの使い方を 100% 把握できるように」→「ローカルだけ短くしてよい」）。前置きを手順・道具の表・決まり・通しの例に置き換え、道具の説明を「何をする・いつ使う・呼び出しの例」に短くする（めったに使わない引数は定義から外すが、ハンドラはそのまま受ける・全部は Tools(names=[...]) で読める）。開発モードで依頼の前に読む量が約 6.4 万字 → 3.1 万字。`full` で今までどおり、`local` で強制。 | `laps/oc/localguide.py:265` | 1 |
| `LAPS_QWEN_SAMPLING` | （未設定＝開発モードは coding・ほかは general） | Qwen3.6 の思考ありの調整値の組（backend.qwen36_sampling・2026-10-18 オーナー「長期タスクでの内部思考の繰り返しなども非常に多く散見」）。作者のカードの「一般の思考」（温度 1.0・presence_penalty 1.5＝終わらない繰り返しを減らす）と「精密なコーディング」（0.6・0）。`coding`／`general` で決め打ち。`LAPS_TEMPERATURE`・`LAPS_PRESENCE_PENALTY` はさらに優先。 | `laps/backend.py:461` | 1 |
| `LAPS_LISTEN_GPU` | 1 | `0` で NVIDIA の GPU があっても Listen を CPU 版で動かす（CUDA 版は 674MB・CPU 版は 9MB）。 | `laps/oc/listen.py:120` | 1 |
| `LAPS_VISION` | 1 | `0` で Read の画像（png/jpg/webp/gif/bmp）を会話に添えない（従来どおり中身の事実だけを返す）。推論機が画像を受けられない（`/props` の modalities.vision が false）ときは、添えずにその旨をモデルへ伝える。 | `laps/oc/tools.py:620` | 1 |
| `LAPS_SKILLS_SH` | 1 | `0` で Skill の検索から外の skills.sh を外す（読むだけの action=read も止める）。外の候補は入れない・本文は非信頼データ・検索語に IP・鍵らしい語・パス・メールがあれば送らない・失敗は失敗と出す（2026-11-06 オーナー「Blackwall エージェントにも導入させて」・find-skills が元）。 | `laps/oc/skilltool.py:50` | 1 |
| `LAPS_GITHUB_API` | https://api.github.com | 外のスキル（skills.sh）の SKILL.md の置き場所を探す GitHub の API の根（2026-11-16・木 git/trees/HEAD を 1 日控える・試験で差し替える）。取れなければ決まった場所を試す。 | `laps/oc/skilltool.py:273` |
| `LAPS_DOOM_LOOP` | 0 | `1` で、同じ呼び出しが 3 回続いたら走らせず、6 回で走行を止める（終わり方「同じ手の繰り返し」）。2026-11-16 オーナー「同じ手の繰り返し…これなくして」「その他の制限も全部取り払って」で既定は外した（1 か数で戻す）。Desktop の look・wait のように同じ引数で毎回違う結果が返る道具を止めていた。 | `laps/engine/loop.py:1578` |
| `LAPS_REPEAT_STOP` | 0 | `1` で、同じ考えの繰り返しを 6 回切ったら走行を止める（切って生成し直すのは既定でも続ける）。2026-11-16 オーナー「同じ手の繰り返し…これなくして」「その他の制限も全部取り払って」で既定は外した（1 か数で戻す）。 | `laps/engine/loop.py:1252` |
| `LAPS_MAX_EMPTY` | 0 | 空の返事（本文も道具も無い手番）を促し直す回数の上限。`0` は上限なし。2026-11-16 オーナー「同じ手の繰り返し…これなくして」「その他の制限も全部取り払って」で既定は外した（1 か数で戻す）。 | `laps/engine/loop.py:1335` |
| `LAPS_MAX_UNREAD` | 0 | 崩れた道具の呼び出しを書き直させる回数の上限。`0` は上限なし。2026-11-16 オーナー「同じ手の繰り返し…これなくして」「その他の制限も全部取り払って」で既定は外した（1 か数で戻す）。 | `laps/engine/loop.py:1309` |
| `LAPS_CHILD_MAX_ITERS` | 0 | 子（Task）の手番の上限。`0` は上限なし（前は 40）。2026-11-16 オーナー「同じ手の繰り返し…これなくして」「その他の制限も全部取り払って」で既定は外した（1 か数で戻す）。 | `laps/engine/loop.py:1778` |
| `LAPS_CHILD_BACKEND` | （空＝子も親と同じ推論機） | 子のエージェント（Task）を動かす推論機の口（OpenAI 互換）。`/health` が応えれば子はそこで動き、親の席は触られない（席の保存と戻しが要らない）。応えない・`off`・空・親と同じ・親が Claude などなら、今までどおり親と同じ推論機。最初の要求で落ちたら親の推論機でやり直す。アプリの設定 `child_backend`。2026-11-25 オーナー「Gyro-M が子に命令を送って作業をさせて結果を受け取れるように。Claude の仕組みに近い感じに」。 | `laps/engine/childengine.py:27` | 1 |
| `LAPS_CHILD_MODEL` | laps-child | 子の推論機に送るモデルの名前（llama-server の --alias と揃える）。 | `laps/engine/childengine.py:66` | 1 |
| `LAPS_CHILD_KEY` | （空） | 子の推論機の API キー（llama-server の --api-key を使う時だけ）。 | `laps/engine/childengine.py:66` | 1 |
| `LAPS_CHILD_CTX_SOFT` | 60000 | 子の文脈の目安（トークン）。越えたら一度だけ「答えられるなら今まとめて」と促し、2 倍でもう一度（止めはしない）。`0` で切る。2026-11-18（子が 15 万トークンまで育った）。 | `laps/engine/loop.py:766` |
| `LAPS_CHILD_RESULT_MAX` | 12000 | 子の答えがこの字数を越えたら全文を L.A.P.S の置き場（`LAPS_HOME`・既定は `~/.laps` か `~/.blackwall`）の `tasks/` に書き、親には頭と置き場を返す。`0` で切る。2026-11-18。 | `laps/engine/loop.py:252` |
| `LAPS_SKILL_GATE` | 1 | `0` でスキルの門を外す（2026-11-16 オーナー「Skills を必ず読ませるようにしたい」「インターネット上の Skills を優先させて」）。20 字以上の新しい依頼では、最初の道具の前に Skill で探し（skills.sh が先・Umbra が後）、読むか、探したうえで `none` と言うまで、ほかの道具を走らせない（2 回止めても選ばなければ通す）。続きの一言・Ask・子では張らない。 | `laps/engine/aids.py:334` |
| `LAPS_SKILL_LIST` | 1 | `0` で前置きのスキルの一覧（名前と一言）を出さない（2026-11-15・強いモデルに一覧から選ばせる）。Umbra の棚は取れたら控え（`~/.laps/skills/umbra/catalog.json`）に書き、取れなければ控えを使う。 | `laps/engine/prompt.py:235` |
| `LAPS_VISION_MAX_PIXELS` | 802816 | Desktop と Browser の look の写真を、モデルに見せる前にこの画素数まで縮める（推論機の画像の読み手は 1 枚 1,024 トークンまで＝28×28 画素ずつ）。縮めたら何 % で見ているかを添える（2026-11-15・labeye の info）。 | `laps/oc/lookaid.py:40` |
| `LAPS_LOOK_GRID` | 1 | `0` で look の写真に 0〜1000 の目盛り（100 ごとの破線と数字）を重ねない（2026-11-15・labeye の grid）。 | `laps/oc/lookaid.py:47` |
| `LAPS_LOOK_SNAP` | 1 | `0` で Browser の x・y の点を近くの枠（16 画素以内）の中心へ寄せない（2026-11-15・labeye の snap）。 | `laps/oc/browsercdp.py:1232` |
| `LAPS_STALE_GUARD` | 1 | 最後に Read・Write・Edit した後でディスクの上で変わったファイル（mtime か大きさが違う）を Write で上書きしない。コマンド・背景の仕事・人が書き換えた中身を、古い記憶で消すのを止める（2026-11-07 オーナー「Blackwall に Qwen Code の良い部分を導入して」・Qwen Code の FileReadCache と priorReadEnforcement に倣う）。読み直せば通る。Edit は今の中身に当てるので見ない。`0` で切る。 | `laps/engine/tools/files.py:211` | 1 |
| `LAPS_SKILLS_SH_URL` | https://skills.sh | 外のスキルの検索の宛先（`/api/search?q=`）。 | `laps/oc/skilltool.py:64` | 1 |
| `LAPS_TOOL_HINTS` | 1 | `0` で道具の案内（依頼の文と照らして合いそうな skill・道具を 1〜3 件、依頼の発言の写しに添える・推論機は呼ばない）を止める。 | `laps/oc/toolhints.py:130` | 1 |
| `LAPS_READING_NOTES` | 1 | `0` で依頼の読み方の注記（短い続きの発言・「どんな…化調べて」の変換違い・作業場に無い名前とよく似た実在の名前）を止める。推論機は呼ばず、依頼の発言の写しにだけ付ける。 | `laps/oc/reading.py:41` | 1 |
| `LAPS_WORKLOG` | 1 | `0` で作業ログ（作業フォルダの `.blackwall/WORKLOG.md`。走行の終わりと、作業フォルダが変わった手番ごとに中核が書く。フォルダの構造・メモ・直近の記録を走行の始めと要約の後の発言に添える。道具ごとに作った・変えた・消したファイルを数えて道具の結果の末尾に 1 行添える＝ファイル 5 万本・2 秒を超える作業フォルダでは数えない・2026-10-12）を書くのも読むのも止める。 | `laps/oc/worklog.py:58` | 1 |
| `LAPS_WORKLOG_EVERY` | 300 | 長い走行の途中で作業ログ（`.blackwall/WORKLOG.md`）に「作業中」の記録を書く間隔（秒）。同じ走行の途中の記録は置き換え、終わりの記録が最後に置き換える。`0` で途中では書かない。 | `laps/engine/memory.py:263` | 1 |
| `LAPS_LOOP_ESCAPE_PRESENCE` | 1.5 | 推論機の側で生成が同じ文・段落・思考の繰り返しに入って切ったとき（loop_cut・思考の長さの天井 long は除く）、**その直後の要求 1 回だけ** presence_penalty をこの値にして送る（普段の手番は今のまま・思考は止めない）。続けて切られたらその次もまた 1 回。`0` で切る。2026-10-15 オーナー「モデルが内部思考をしすぎて迷走していないか確かめて」→「進めてください」: 14:48 の走行は繰り返しで切った要求が 7 本・464 秒（約 4 割）で、やり直しの要求がまた同じ繰り返しに入った（3 本・4 本と続けて切られた）。Qwen3.6 のカードの「一般の思考向け」は presence_penalty 1.5、Qwen3 のカードは「0〜2 で終わらない繰り返しが減る・高すぎると言語が混ざる」。走行記録の `engine:loop_escape`。 | `laps/backend.py:1215` | 1 |
| `LAPS_LOOK_STREAK` | 0（新しい中核） | oc で、何も変えずに見るだけの呼び出し（読む・一覧・検索・聞く）がこの回数続いたら、道具の結果の末尾へ「決めて動くか、行き詰まりを利用者へ伝える」という一言を足す（同じ回数ごとに 1 回）。`0` で足さない。 | `laps/engine/aids.py:713` | 1 |
| `LAPS_STALL_SEC` | 300 | 背景の仕事（run_in_background の pid と、自動で背景に回った J1…）が、出力も CPU もこの秒数動かなければ「進んでいない（固まった・入力待ち・ロック・通信待ち）」と知らせる（oc/jobwatch・2026-10-28 オーナー「スクリプトのプロセスが途中で止まっているのに気づかなかった」）。出力は命令の書き込み先（`> log 2>&1`・tee・Out-File）も見る。何も書かずに計算している物は CPU で進んでいると見る。入力を求める文（[y/N]・Password: など）で止まっていれば 20 秒で知らせる。終わったら終了の番号と出力の末尾を知らせる。`0` で停滞の知らせだけ切る。数え: `bg_exit`・`bg_stall`・`bg_prompt`・`bg_resumed`。 | `laps/oc/jobwatch.py:41` | 1 |
| `LAPS_BG_PARK` | 1 | 答えを書いて手番を終えても背景の仕事（サーバー以外）が動いていれば、走行を閉じずに推論機を使わずに待ち、仕事が終わる・進まなくなる・入力を待つ・利用者が話しかける、のどれかでモデルを起こす（Claude Code と同じく、背景の仕事の間も話しかけに答え、終われば知らせる・2026-10-28 オーナー「バックグラウンドで実行している間にこちらの呼びかけに応答できるように」）。止める（取り消し）を押せば走行を終えて仕事も止め、止めた仕事を答えに書き添える。`0` で切る（従来どおり走行の終わりに止める）。数え: `bg_park`・`bg_park_woke`・`bg_park_woke_user`・`bg_park_secs`・`jobs_stopped_footer`。 | `laps/engine/loop.py:944` | 1 |
| `LAPS_BG_PARK_MAX_SEC` | 21600 | 走行を閉じずに待つ間、何も起きなくてもこの秒数ごとにモデルを起こして、どこまで進んだかを利用者へ言わせる（既定 6 時間）。`0` で起こさない。 | `laps/engine/loop.py:950` | 1 |
| `LAPS_AUTO_BG_SEC` | 30 | oc で、前で走らせた Bash がこの秒数を超えたら、止めずに背景の仕事（J1・J2…）として続け、モデルには先に「続いている」と返す。その間モデルは別の作業や利用者への返事ができ、結果は終わった後の道具の結果の末尾に添える（BashOutput(pid="J1", wait=…) で待つ・kill=true で止める）。走っている仕事の一覧を毎回添え、同じ命令の二重起動を止め、残したまま Done を呼んだら 1 回確かめ、走行の終わりに止める。子（Explore）の中では使わない。強制の割り込みは前の 1 歩だけを止め、背景の仕事は続ける（2026-10-18 オーナー「プログラム実行中とかの間にも応答したりできる…ローカル LLM 向けに」）。`0` で切る。数え: `auto_bg`・`auto_bg_delivered`・`auto_bg_dup_blocked`・`auto_bg_done_nudge`・`jobs_stopped_at_end`・`preempt_kept_jobs`。 | `laps/engine/tools/shell.py:700` | 1 |
| `LAPS_EXPLORE_FIRST` | 0 | ★2026-11-09 から既定は切る（オーナー「このエージェントがタスク前に動くこと自体いらないかも」）。`1` で、会話の最初の依頼のとき、最初の手番の前にハーネスが子（Explore）を 1 回走らせ（手番 8 まで）、その結果を「親が Explore を呼んで返ってきた結果」の組として会話に入れる（2026-10-18 オーナー「子を最初から使わせる仕組みを導入してください」・渡しても呼ばなかったため）。子が「見る物が無い」と答えた・失敗した・作業フォルダが空のときは入れない。`every` で依頼ごと。数え: `explore_first:ran`・`explore_first:nothing`・`explore_first:failed`・`explore_first:empty_folder`・`explore_first_sec`。 | `laps/oc/explore.py:170` | 1 |
| `LAPS_EXPLORE_FIRST_SECONDS` | 45 | 最初の下見の子（`LAPS_EXPLORE_FIRST`）の持ち時間（秒）。過ぎたら子は道具を使わずに、それまでに分かったことをまとめて終える（手番の上限で止めるときと同じ最後の手番）。`0` で上限なし（手番 8 までだけ）。2026-10-22 Terminal-Bench の診断の走行で、制限 360 秒の課題で下見が 155 秒・126 秒を使い（子が課題そのものを解き始めた）、どちらも時間切れで落ちた。数え `deadline_hit`（子の側）。 | `laps/oc/explore.py:145` | 1 |
| `LAPS_MODE` | general | 作業のモード（oc/modes）。`general`＝今までどおり全部／`development`＝開発（Monitor・Serve を足し、解析の節を外す）／`modeling`＝3D モデリング（Monitor・App を足し、3D の節を入れ、解析の節を外す）／`analysis`＝解析（★枠だけ・既定は general と同じ）。渡す道具と前置きの節が変わる。★外した道具は消えない（`Tools` を通して呼べる）。走行の要求の `mode`・設定の `mode` でも選べる。★2026-10-19 アプリの Mode の札は外した（オーナー「モードはモデル自身に考えさせましょう。こっちで制御を入れると意図しない動作をしそう」）＝既定の general で全部渡し、何を使うかはモデルが選ぶ。2026-10-15 オーナー「機能を詰め込みすぎるとあれなので、分類しましょうか。解析モード(analysis)・開発モード(development)・3D モデリングモード(Modeling)でそれぞれ作業に特化させる形で」。 | `laps/engine/server.py:130` | 1 |
| `LAPS_MODE_<名前>_TOOLS` | （無し） | そのモードで渡す道具をカンマで並べて上書きする（例 `LAPS_MODE_ANALYSIS_TOOLS=Bash,Read,Grep,Glob,Umbra,Lab,Skill,TodoWrite`）。枠だけの `analysis` を絞るのに使う。空・書き損じで 0 本になったら絞らない（走行を止めない）。 | `laps/oc/modes.py:74` | 1 |
| `LAPS_PROMPT_BASE` | merged | 前置きの本文と道具の説明（Bash・Read・Write・Edit・Glob・Grep）の形。`merged`＝opencode の本文と指図を土台に Blackwall で足した物（確かめ方・既成の道具を先に探す・帳面・解析の作法・言語・OS ごとの道具の例・棚と 作業用のコンテナ）を混ぜた形。ぶつかる所は「1 つのファイルの読み書き・編集は専用の道具、多数のファイルや形式をまとめて扱う既成の道具は Bash で可」にまとめた。`opencode`＝0.1.503 より前の文面そのまま、`blackwall`＝0.1.503〜0.1.507 の文面。2026-10-15 オーナー「挙動がおかしいです。一旦 Opencode 由来のものをサブとして戻します」「Opencode ベースで Blackwall も混ぜる感じでいいです」。 | `laps/oc/promptbase.py:28` | 1 |
| `LAPS_PROMPT_PROGRESS` | 1 | `0` で推論機に読み込みの進み具合（llama.cpp `return_progress`）を送らせない。送らせると、読み込みの間も心拍に「読み込み 34%（…・残り約 2 分）」が載る。 | `laps/backend.py:204` | 1 |
| `LAPS_HOSTLOAD` | 1 | `0` で、道具の結果の頭に添えるこの機械の資源の一行（`[Host]`: CPU・メモリの空き・ディスクの空き・GPU）を切る（oc/hostload）。添えるのは走行の最初・限界に近い物が出たとき・60 秒以上の道具の後・定期（下）。Lab（作業用のコンテナ）の結果には付けない。2026-10-13 オーナー「実行されているホストのリソース状況を確認しながら作業を進めさせる仕組みも追加」。 | `laps/oc/hostload.py:44` | 1 |
| `LAPS_HOSTLOAD_EVERY` | 600 | `[Host]` の一行を定期に添える間隔（秒・60 以上）。 | `laps/oc/hostload.py:49` | 1 |
| `LAPS_SCRATCH_TIDY` | 1 | `0` で、走行の終わりに「この走行で作業場の直下に新しく作った試しの台本（名前が check_・debug_・..._v2 などの形の .py・.ps1・.js など）」を `_scratch` へ移すのを切る。最後の答えで名前を挙げた物・前からある物は動かさない。同じ名前があれば上書きせず別名。2026-10-13 オーナー「任せます」（直下に置いたその場の一言に、Gemma が 5 回とも従わなかった）。 | `laps/engine/finish.py:478` | 1 |
| `LAPS_EXPLORE` | 1 | `0` で、調べ物を任せる子エージェントの道具 `Explore` を外す。★2026-10-18 からローカルの小さいモデル向けに作り変えて既定で入れる（オーナー「ローカル LLM なので ClaudeCode と全く同じ仕組みにするとよくないことになりそうなので合わせて作ってみて」）: 子への頼みにハーネスが利用者の元の依頼と親がもう読んだファイルを添える／子は ANSWER・EVIDENCE（path:line と引用）・NOT FOUND の形で答え、ハーネスが引用を実ファイルと突き合わせて ✓ / ✗ を付ける／子は 12 手番まで・読む・探す・見るだけの道具（Read・Glob・Grep・Bash・BashOutput・WebSearch・WebFetch・Umbra・Lab）・答えは 4,000 字まで・入れ子にしない。旧（10-15）は既定で切っていた（当時は推論機の枠が文脈の置き場を共有していた）。数え: `explore_calls`・`explore_child_tools`・`explore_chars`・`explore_evidence_ok`・`explore_evidence_bad`。 | `laps/oc/explore.py:126` | 1 |
| `LAPS_TOOL_OUTPUT_ADAPT` | 1 | `0` で、道具の出力の上限を会話の長さで下げるのを切る（常に `LAPS_TOOL_OUTPUT_KB`）。推論機が読んだ量が 3 万トークン以上で 10KB・5 万以上で 8KB（全文はファイルに残る）。2026-10-13 オーナー「長時間の作業にも耐えられるような設計に」（108 分の走行で読み込み待ちが 41 分・上限いっぱいの結果 1 つで 37〜84 秒）。 | `laps/engine/tools/__init__.py:112` | 1 |
| `LAPS_THINK_HEDGE` | 15 | 新しい中核で、思考の直近 40 文のうち迷いの書き出し（しかし・または・おそらく・However・Maybe など）がこの数に達したら思考を切って「道具で確かめるか、決めて動く」と伝える。`0` で切る（2026-10-31 オーナー「しかし、おそらくと言ってずっと考えている」）。 | `laps/engine/llm.py:387` | 1 |
| `LAPS_THINK_CEILING_CHARS` | 12000 | Gemma・GLM・Qwen の MoE（2026-10-14〜・Qwen3.6-35B-A3B）で、1 手番の思考がこの字数を超えたら区切って、行動するよう一言促す（これらの系統は推論機の思考の予算で手番ごとに打ち切らないため）。普段の思考には触らない高さ（661 手番: 中央値 742 字・上位 1% 10,312 字・超えたのは 0.6%）。`0` で切る。2026-10-13 オーナー「任せます」（本文を出さずに思考が 1.9 万字まで伸びた手番があった）。 | `laps/backend.py:177` | 1 |
| `LAPS_REPEAT_PENALTY` | 1.0 | 反復ペナルティの上書き。`repetition_penalty`（HF/vLLM の綴り）と `repeat_penalty`（llama.cpp の綴り）の**両方**で送る。2026-09-09 まで前者しか送っておらず、llama.cpp はそれを黙って捨てるため実効値は既定 1.0 のままだった（実測: `/slots`）。 | `laps/backend.py:519` | 1 |
| `LAPS_SEED` | (未設定=毎回ランダム) | サンプリング種。設定したときだけ payload に載せる（既定で決定論にしない——同じ質問を言い直せば違う答えが返る、という不具合回避手段を利用者から奪わないため）。⚠ 実測: 手元の llama-server は同じ種でも出力が揺れた（cont-batching 由来と推測・未検証）。 | `laps/backend.py:583` | 1 |
| `LAPS_TEMPERATURE` | (未設定→モデル依存/0.7 系) | サンプリング温度の上書き。 | `laps/backend.py:619` | 1 |
| `LAPS_THINK` | 1相当(既定 ON) | `0` で thinking を無効化（`/no_think` と同じ効果）。既定 ON——OFF は tool-call 精度を大きく落とすと実測されている（bench 27%まで低下）。 | `laps/backend.py:337` | 1 |
| `LAPS_SHOW_THINK` | 既定 出さない（秒だけ） | 端末版で思考の全文を流すか。`1` で全文・`0` で出さない（生成は止めない＝`LAPS_THINK` とは別）。★2026-10-30 オーナー「内部思考を出さないようにしてモデルに対して、タスク状況を報告しながら進めるように」で既定を出さないへ戻し、代わりにモデルが道具の欄に書いた一文（`LAPS_PROGRESS`）を出す。`LAPS_PROGRESS=0` のときは前どおり全文（2026-09-21 オーナー「でていないと推論しているんだかしていないんだかわかりません」）。 | `laps/term/render.py:863` | 1 |
| `LAPS_REASONING_BUDGET` | max_tokens の60%・天井 1536 | 思考に許すトークン数を**本文と分けて**渡す（llama.cpp の `reasoning_budget_tokens`。達したら `reasoning_budget_message` を差し込んで思考を閉じる）。絶対値を指定・`0` で無効。★2026-10-06: 比率だけでは oc（max_tokens 16,384）で 9,830 になり、1 回の応答が 6,575〜8,687 トークンまで暴走した→天井 1536。 ★新しい中核（engine/llm.py）の既定は Think Low 2048・無指定 8192・Think High 16384（2026-10-31）。 | `laps/backend.py:140` | 1 |
| `LAPS_AUTO` | 1 | 作業係を auto で動かす（既定）。前置きに「許可や確認を求めずに、コマンドの実行・ファイルの作成と変更・次の一手を進める。依頼そのものが曖昧なときだけ聞く。頼まれていないデータを消すしかないときは控えを取る・新しいファイルに書く」の節を足し、作業の途中（その走行でファイルを書き換えた後）で「〜してもよろしいでしょうか？」と許可を求めて手番を終えたら、考えさせずに道具で進めさせる（1 走行 3 回まで）。危ない命令はモデルの外（policy）で止める。2026-10-12 オーナー「作業係は auto 機能をデフォルトにしてください。ユーザーに許可を求めないように」。`0` で従来どおり（確認してから進める）。 | `laps/oc/prompt.py:304` | 1 |
| `LAPS_NOTEBOOK` | 1 | 道具が読んだ構造（Read のバイナリの構造・find・path_id・モデルの要約・DLL の型）を作業フォルダの `.blackwall/analysis/` に対象と読み方ごとの .md で保存し、`INDEX.md` に一覧を作る（同じ対象と読み方は上書き・上限 400 枚）。結果の末尾に保存先を 1 行添える。`0` で止める。★2026-10-11 オーナー「解析だけして結果はどこにも保存してない？」→「この案で」。 | `laps/oc/notebook.py:34` | 1 |
| `LAPS_LESSONS` | 1 | 同じ失敗の知見を走行をまたいで貯める（2026-10-20 オーナー「Web検索をして知見を貯める動作等入れたいです。タスクのときに同じ失敗を繰り返してしまって詰まるときが結構多いので」）。①同じ型の失敗の 2 回目で、まだ Web を引いていなければ失敗の行から作った問いで `WebSearch(query="…")` を名指しする（1 走行で同じ型に 1 回）②同じ型で 2 回以上失敗した後、同じ種類の手（Bash は命令の頭の語）が成功したら、その手と間に引いた問い・読んだ URL を `~/.blackwall/lessons.jsonl`（`LAPS_HOME`）へ積む（redact を通す・400 件まで）③失敗したとき帳面に同じ（近い）型があれば「前の走行で効いた手」を添える（1 走行 4 回まで）。子（Explore など）では動かない。推論機は呼ばない。`0` で全部止める。 | `laps/oc/lessons.py:42` | 1 |
| `LAPS_FINDINGS` | 1 | 長い作業の記憶（2026-10-15 オーナー「当初の目的を忘れてしまったり、自分で解析した結果を忘れてしまったり」）。①要約のたびに要約の「確かめた事実」「行き止まり」を作業フォルダの `.blackwall/FINDINGS.md` へ積み足す（縮めない・同じ事実の言い換えは識別子と文の近さで足さない）②要約で縮める前の会話（思考を除く）を `.blackwall/history/<会話>.md` に控え、要約の後の一言に場所を書く③要約の後に帳面と ToDo の一覧を添え、新しい会話の最初にも帳面を添える④長い走行では 20 手番ごとに元の依頼・ToDo の残り・帳面の件数を道具の結果の末尾に添える。`0` で全部止める。 | `laps/oc/findings.py:69` | 1 |
| `LAPS_TOOLBOX` | 1 | 起動のたびに（`oc/parts`）定番の道具を道具の置き場へ入れる（公開版では何もしない）（rg・fd・jq・yq・sd・7zr・7zip・exiftool・MediaInfo・DetectItEasy。PATH に既にある物・入れてある物は入れない・Umbra に届かなければ何もしない・2026-10-15 オーナー「既成のツールをもっと使うような仕組みに」）。`0` で止める。カンマで棚の名前を並べると、その組に替わる。置き場の道具は `<env>` に名前が出る。 | `laps/oc/toolenv.py:217` | 1 |
| `LAPS_USER_PYTHONPATH` | （起動元が入れる） | 人が設定する物ではない。term の client とアプリの gateway が、本体を見つけるために PYTHONPATH を書き換える前の値（空＝元は無かった）を残す。Bash の道具など子プロセスに渡す環境では PYTHONPATH をこの値へ戻す（oc/toolenv.child_env）。2026-10-22 Terminal-Bench の fix-pandas-version: 書き換えた PYTHONPATH（/usr/lib/python3.11 と同梱の venv）が道具に伝わり、課題の Python 3.8 が起動できなかった。 | `laps/oc/toolenv.py:195` | 1 |
| `LAPS_TOKENIZE_TIMEOUT` | 10 | `/tokenize` 呼び出しのタイムアウト秒（2026-09-02 に 30→10、健全な応答は 104ms）。 | `laps/context_budget.py:353` | 1 |
| `LAPS_TOP_K` | 20 | サンプリング top_k の上書き。 | `laps/backend.py:522` | 1 |
| `LAPS_TOP_P` | 0.95（思考 on）／0.8（off） | サンプリング top_p の上書き（既定は Qwen3.8 の作者公開値・思考の有無で分ける）。 | `laps/backend.py:521` | 1 |
| `LAPS_MIN_P` | 0.0 | サンプリング min_p の上書き（llama.cpp の既定 0.05 を使わず、Qwen3.8 の推奨 0）。 | `laps/backend.py:525` | 1 |
| `LAPS_PRESENCE_PENALTY` | 0.0（思考 on）／1.5（off） | presence_penalty の上書き（Qwen3.8 の推奨・思考の有無で分ける）。 | `laps/backend.py:526` | 1 |
| `LAPS_REASONING_EFFORT` | low | 思考の深さ（Qwen3.8 の書式の `reasoning_effort`: xhigh / medium / low）。思考 on のときだけ送る。★P40 では生成が 10〜15 tok/s。書式の既定 xhigh は 1 手が何分にもなり、medium は何も足さない（モデル素のまま・実測 1 手番 713 tok・85 手番 102 分）ので既定は low。 | `laps/backend.py:184` | 1 |
| `LAPS_OC_SKILLS` | 1 | oc の Skill 道具（同梱・利用者・作業場の SKILL.md を要るときに読み込む）を載せるか。"0" で外す。 | `laps/engine/tools/__init__.py:214` | 1 |
| `LAPS_TOOLS_DIR` | ~/.laps/tools・旧い ~/.blackwall が在ればその中（Windows のアプリは `<データ根>\tools` を渡す） | 道具の置き場。中の実行ファイルのある所（深さ 3 まで）を Bash の子プロセスの PATH の先頭に足す（oc）。 | `laps/engine/tools/files.py:660` | 1 |
| `LAPS_TOOL_OUTPUT_KB` | 16 | oc の道具（Bash・Read・Grep・WebFetch）がモデルに渡す出力の上限 KB（4〜1024）。超えた分は全文をファイルに残し、Bash は先頭 1/4 と末尾を渡す。★P40 の読み込みは毎秒 100〜120 トークン＝50KB（旧値）近い出力は考え始めるまで 2〜2.5 分かかっていた。 | `laps/engine/tools/__init__.py:108` | 1 |
| `LAPS_RECORD` | 未設定（off） | 端末版の録画向け表示（--record / --demo）。カード表示＋波を止め、状態行は 10 秒に 1 回だけ描き直す（asciinema の --idle-time-limit で待ち時間を詰められる）。 | `laps/term/render.py:190` | 1 |
| `LAPS_PRIVACY` | 未設定（off） | 端末版の伏せ表示（--privacy / --demo）。IP・ホームのパス・ユーザー名・ホスト名を伏せ字にする（例示用の予約 IP と 127.* は残す）。 | `laps/term/render.py:194` | 1 |
| `LAPS_TUI_STYLE` | 未設定（行の形） | `card` で端末版をカード表示（--card）＝Blackwall の意匠（起動の大きな銘・暗い赤の枠と区切り線・種類の札・答えの前の銘）。銘は 77 桁未満では 1 行に落とす。★罫線と `█` は曖昧幅の字なので、曖昧幅を 2 桁にしている端末では枠と銘が崩れる。 | `laps/term/render.py:198` | 1 |

## 走行の上限と委譲

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_BASH_TERSE` | (未設定) | Bash 道具の出力を簡潔にするモード。検収の実行中だけ立てて必ず戻す（戻し忘れが実際に不具合を生んだ箇所と同型）。 | `laps/tools/bash.py:668` | 2 |
| `LAPS_PLAN_REMIND` | 1 | モデルが自分で作った予定表（TodoWrite）に終わっていない項目があり、作業の道具を 8 回呼ぶ間更新されていないとき、道具の結果の末尾に「予定表は N 回更新されていない・終わっていれば今更新して」を一言添える（止めない・1 走行 6 回まで・予定表が無ければ何もしない）。`0` で切る。2026-10-23 オーナー「プラン状況の更新が入るのがタスク終了時のみかもしれません」（開発機 の記録で 48〜50 手番更新しない区間・最後にまとめて ✓）。数え `plan_stale_note`。 | `laps/engine/aids.py:693` | 1 |
| `LAPS_BWTOOL` | 1 | モデルが書くスクリプトの中から Blackwall の道具を呼ぶ口 `bwtool`（laps/cli_bwtool.py・oc/bwtool.py）。走行の始めに道具の置き場の `blackwall/` に起動の台本（`bwtool`・Windows は `bwtool.cmd`）を置き、前置きに一行足す。呼べるのは WebSearch・WebFetch・Umbra・Read・Listen・Doc・Construct・Api・Lab（シェルで代わりがきかない物）。readonly では読むだけの道具だけ。呼んだ記録は `~/.blackwall/bwtool.jsonl`、Bash の後に数えて画面に出す（数え `bwtool:<道具>`）。`0` で切る（台本を置かず前置きにも書かない）。2026-10-23 オーナー「ツールをモデルがスクリプトを書く際にそこに組み込めるように」。 | `laps/oc/bwtool.py:44` | 1 |
| `LAPS_BWTOOL_TAG` | （走行の始めに作る） | `bwtool` の呼んだ記録に付ける、この走行（のプロセス）の印。走行の始めに無ければ `<pid>-<乱数>` を作って環境に置き、Bash の子が受け継ぐ。Bash の後の数えは、印が同じ記録だけを数える（同じ機械で同時に走る別の会話の呼び出しを混ぜない）。人が設定する物ではない。2026-10-25 敵対レビュー。 | `laps/oc/bwtool.py:65` | 1 |
| `LAPS_TIME_LIMIT_SECONDS` | 0 | 呼び出し側が知っているこの走行の締め（秒）。0 で無し（アプリ・TUI は渡さない）。渡すと、依頼の写しに「持ち時間 N 秒・それまでに書いた物だけが数えられる」を一行付け、半分と 8 割を過ぎた最初の道具の結果に残りの秒を 1 回ずつ足す。止めはしない（`LAPS_MAX_SECONDS` は進んでいる間は延びる柔らかい予算で、締めではない）。2026-10-22 Terminal-Bench の診断の再走行で、chess・pdp11・raman・polyglot が依頼のファイルを書かないまま時間切れで落ちた。数え `time_note_50`・`time_note_80`。★2026-10-23: 900 秒未満を渡すと最初の下見（`LAPS_EXPLORE_FIRST`）をしない（45 秒の上限でも まとめの手番を足すと 60〜90 秒かかり、制限 360 秒の課題の 2 割を使っていた・数え `explore_first:short_limit`）。 | `laps/engine/aids.py:85` | 1 |
| `LAPS_MAX_SECONDS` | **3600**（oc は 7200） | 1 走行の壁時計時間の上限（秒）。0 以下で無制限。★アプリは設定 max_seconds（既定 0＝無制限）を毎回渡す。oc の既定は 2026-10-06 に 7200。 | `laps/engine/server.py:601` | 1 |
| `LAPS_MAX_TOOL_CALLS` | **400** | 1 走行で許す道具呼び出し回数の上限。0 以下で無制限。 | `laps/engine/server.py:602` | 1 |
| `LAPS_SESSION_ID` | (未設定) | TodoWrite/Task 系の道具が読む「いまのセッション」。未設定だと`no active session` を正直に返す（一部の道具はこれが揃っていなかった不具合の教訓あり）。 | `laps/tools/task_mgmt.py:37` | 3 |

## 道具（laps/tools/**）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_ALLOW_PRIVATE_FETCH` | (未設定=OFF) | `1` で WebFetch がプライベート/ローカルアドレスへも取得できるようにする（既定は SSRF 対策で拒否）。 | `laps/oc/mcp.py:496` | 1 |
| `LAPS_BASH_EXIT_TAG` | 1 | 非ゼロ終了かつ出力ありのとき、末尾に exit タグを付けて成功と区別しやすくするか。 | `laps/tools/bash.py:4419` | 1 |
| `LAPS_BASH_RELEASE` | 1 | 前面実行で**シェルの終了**を待つか（`0` で旧挙動＝`communicate` でパイプの EOF を待つ）。既定 1。`0` にすると、背景に回した子がパイプを持ったままのとき timeout まで待ち、最後にプロセスグループごと SIGKILL する（＝自分で起動したサーバを殺す）。切り分け用の退避口で、常用しない。 | `laps/tools/bash.py:4354` | 1 |
| `LAPS_BASH_RELEASE_GRACE` | 0.5 | シェルが終わったあと「まだ出力を掴んでいるプロセスが居る」と見なすまでの猶予（秒）。goose の `OUTPUT_DRAIN_TIMEOUT_MILLIS=500` に合わせた既定。 | `laps/tools/bash.py:312` | 1 |
| `LAPS_BG_KEEP_HOURS` | 72 | 背景処理の控え（`~/.blackwall/bg/<pid>.{out,err,meta.json}`）を、**終了済みかつこの時間より古い**ものだけ片付ける。`0` で掃除を止める。★消しすぎない設計: 走行中のモデルは終わった処理の出力を `BashOutput(pid=…)` で読み返す。 | `laps/procutil.py:1071` | 1 |
| `LAPS_PROTECTED_PORTS` | 22,18301,18400 | **撃たないポート**（カンマ区切り・指定すると既定を置き換える／空文字で保護なし）。既定は sshd・閲覧用 socat 中継・Android ゲートウェイ（2026-09-03 の dogfood で実際に gateway を撃って本番を壊した）。★2026-09-05（第4回c）に**適用範囲を広げた**: `BashOutput(port=…, kill=true)` だけでなく、`Bash` の名前撃ち（`pkill -f …`）と明示 PID 撃ち（`kill <pid>`）の両方が、このポートを LISTEN しているプロセスとそのプロセスグループを撃たない（`procutil.protected_port_pids()`）。★18300 は入れない（走行が自分で建てるサーバで、片付けられないと工程4 の壁が再来する）。 | `laps/procutil.py:322` | 1 |
| `LAPS_DEAD_TOOLS` | (未設定=死んだ宣言を除外, "1"で残す) | 既定の道具表から「死んだ」宣言済み道具を下ろす機構を無効化するデバッグ用フラグ。 | `laps/tools/__init__.py:365` | 1 |
| `LAPS_EDIT_FUZZY` | 1 | Edit の曖昧一致（行末空白/行内空白の畳み/先頭尾一意区間）を許すか。 | `laps/tools/edit.py:359` | 1 |
| `LAPS_EDIT_SYNTAX_GATE` | 1 | 編集直後に構文チェックを走らせ、壊れた .py/.php 等を弾くか（Edit と ApplyDiff 共通）。 | `laps/tools/apply_diff.py:350` | 2 |
| `LAPS_EDIT_FIT_INDENT` | 1 | 構文の門が拒否した Edit を、new_string を周りの字下げに合わせた版で一度だけ試す（構文が通るときだけ採り、結果に書く）。 | `laps/tools/edit.py:821` | 1 |
| `LAPS_EXT_SYNTAX` | 1 | 拡張子別の外部構文チェッカ（`_EXT_CHECKERS`）を使うか。ツールが無ければ自動で無効。 | `laps/tools/edit.py:116` | 1 |
| `LAPS_GREP_TIMEOUT` | 30 | Grep 道具のタイムアウト秒。 | `laps/tools/grep.py:215` | 1 |
| `LAPS_HEADLESS` | (未設定=OFF) | `1` で AskUser を対話なしモードに（無人実行向け）。 | `laps/tools/ask_user.py:33` | 1 |
| `LAPS_READ_BINARY_GUARD` | 1 | バイナリファイルを Read で開いたとき、文字化けさせず断って開き方を教えるか。★実測: works.db 等を14〜23回誤読し、出力の29.6%がゴミになった不具合の対策。 | `laps/tools/read.py:761` | 1 |
| `LAPS_READ_SQLITE` | 1 | Read が SQLite を開いたら、断らずに表・列・行数を読み取り専用で返す（開けなければ従来の案内）。 | `laps/tools/read.py:750` | 1 |
| `LAPS_PATHFIX` | 1 | 実在しないパスを、見た目が同じで文字だけ違う（全角空白・全角英数など）実在のパスに直す（Read/Edit/Write/Grep/Glob/Bash・候補が 1 つのときだけ）。 | `laps/tools/pathfix.py:38` | 1 |
| `LAPS_READ_IMAGE_MAX_B64` | (実装既定値) | 画像を base64 埋め込みする上限サイズ。multimodal バックエンド接続時に緩める用途。`0` で埋め込み禁止。 | `laps/tools/read.py:189` | 1 |
| `LAPS_WALK_SECS` | 10 | **歩く道具の持ち時間（秒）。`0` 以下で無制限。** Glob の走査と、Read の「近いもの」「同名の別ファイル」探しが見る。★2026-09-06 owner 実機 P0（`D:\Blackwall` で道具が `(running)` のまま戻らない）の対策。件数の上限は**一致した数**しか数えないので、`**/*.exe` のように一致が 0 件のまま木を歩き続ける形では一度も発火しない。Grep は自前で `LAPS_GREP_TIMEOUT` を持っているのでこれを見ない。 | `laps/engine/tools/files.py:623` | 1 |
| `LAPS_SEARCH_BACKEND` | ddg | WebSearch のバックエンド選択。 | `laps/tools/websearch.py:158` | 1 |
| `LAPS_SHELL` | (未設定→bash があれば bash) | Bash 道具が実際に呼ぶシェルの明示指定。 | `laps/tools/bash.py:741` | 2 |
| `LAPS_SYSSTATE` | 1 | focused ツールセットに SystemState を含めるか（既定 ON・A/B 測定用）。 | `laps/tools/__init__.py:358` | 1 |
| `LAPS_YES` | (未設定=OFF) | `1` で確認プロンプトを自動 yes（`permission.py`）。CLI の `--yes`/`/update` 等が立てる。 | `laps/permission.py:64` | 2 |

## 編集後チェック（postedit.py）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|

## チェックポイント／控え（checkpoint.py）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_CHECKPOINT` | 1 | 編集直前のバイト列を控える機構（copy-on-first-write）の既定 ON/OFF。 | `laps/checkpoint.py:1621` | 1 |
| `LAPS_CHECKPOINT_DIR` | ~/.laps/checkpoints（旧い ~/.blackwall が在ればその中） | 控えの置き場所の上書き。LAPS_HOME が作業場所の中を指すベンチ構成のときは作業場所を汚さず一時ディレクトリへ自動退避。 | `laps/checkpoint.py:1598` | 1 |
| `LAPS_CHECKPOINT_KEEP_HOURS` | 48 | `begin()` の時点で、これより古い控えを掃除する保持時間。 | `laps/checkpoint.py:1659` | 1 |
| `LAPS_CHECKPOINT_MAX_FILES` | 500 | 1 依頼ぶんで控えるファイル数の上限。 | `laps/checkpoint.py:146` | 1 |
| `LAPS_CHECKPOINT_MAX_FILE_BYTES` | 8MiB | 1 ファイルぶんの控えの上限バイト数。 | `laps/checkpoint.py:142` | 1 |
| `LAPS_CHECKPOINT_MAX_TOTAL_BYTES` | 64MiB | 1 依頼ぶんの控え合計の上限バイト数（依頼開始時点＋ウォーターマーク＋復元前の控えの**合計**）。 | `laps/checkpoint.py:144` | 1 |
| `LAPS_CHECKPOINT_MAX_TREE_FILES` | 5000 | 作業場を木ごと控えるとき（`LAPS_ACCEPTANCE_HOLD_SCOPE=tree`）の上限ファイル数。超えたら木の控えは諦めて追跡済みだけ控える（部分的な木は作らない）。 | `laps/checkpoint.py:148` | 1 |

## セッション・LAPS_HOME（session.py / cli.py / hooks.py 等）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_MAX_ITERS` | 0 (=上限なし) | ★盲点だった変数(2026-09-04): `os.environ` を直接読む箇所は無く、`laps/term/config.py` の `ENV_KEYS`（env名→設定鍵の対応表）を `resolve()` が `for var, key in ENV_KEYS.items(): e.get(var)` と回して読む——AST 走査で単発の `.get("LAPS_X")` だけを追っていたスキャナには最初見えなかった（`tests/_env_scan.py` を dict-fanout 対応に拡張して発見）。1送信あたりの反復上限。端末クライアント（`blackwall`／`term/app.py`）の設定解決経由でだけ効く——`blackwall-legacy` や `serve.py`/`gateway.py` はこの変数を読まない。 | `laps/term/config.py:362` | 1 |
| `LAPS_NEW_SESSION_ID` | (未設定) | 新規セッションを作るときの ID を強制する（テスト/再現用）。「いま何か」と「これから何を作るか」を1変数に混ぜない、という注記つき——過去に混ぜて実機 0 秒失敗を踏んだ。 | `laps/session.py:87` | 1 |
| `LAPS_PROSE_WIDTH` | 0 (=端末幅いっぱい) | 本文（道具の出力・コード・差分・表は除く）の読み幅（表示セル数）の上書き。上と同じ `ENV_KEYS` 経由の盲点。実機の端末が設計時に検証した幅(20/40/80/100)の外（200桁）で、折り返し設計が効く場所に文字が来ていなかった不具合の対策として追加。 | `laps/term/config.py:362` | 1 |
| `LAPS_SESSIONS` | <HOME>/sessions | セッション記録の置き場所の上書き（run が消せない場所へ逃がす用途）。 | `laps/session.py:57` | 1 |
| `LAPS_TELEMETRY` | (空＝送らない) | `/sendlog` の送り先。上と同じ `ENV_KEYS` 経由の盲点。手動コマンドでしか送信されない（勝手に送らない、と README が明記）。 | `laps/term/config.py:362` | 1 |

## serve / gateway（HTTP・WebSocket サーバ）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_GATEWAY_GRACE` | 600 | 切断後に serve プロセスを生かしておく猶予秒数（再接続用）。 | `laps/gateway.py:879` | 1 |
| `LAPS_GATEWAY_HOME` | /root/.laps-mobile（旧い /root/.blackwall-mobile が在ればそのまま） | ゲートウェイ経由の既定 LAPS_HOME。 | `laps/gateway.py:877` | 1 |
| `LAPS_GATEWAY_HOST` | 0.0.0.0 | ゲートウェイの bind ホスト。 | `laps/gateway.py:874` | 1 |
| `LAPS_GATEWAY_MAX` | 4 | ゲートウェイが同時に抱える接続数の上限。 | `laps/gateway.py:878` | 1 |
| `LAPS_GATEWAY_PORT` | 18400 | ゲートウェイの bind ポート。 | `laps/gateway.py:875` | 1 |
| `LAPS_GATEWAY_TOKEN` | (未設定→生成して deploy/gateway.token へ保存) | モバイル/リモート向け WebSocket ゲートウェイの認証トークン。空文字は「認証なし」。 | `laps/gateway.py:851` | 1 |
| `LAPS_RELAY_URL` | ws://127.0.0.1:18400 | この機械（remotehost）が名乗りに行く中継（自分で立てた relay.py）の場所。Windows のアプリは設定 `remote_relay` から渡す。 | `laps/relayauth.py:103` | 1 |
| `LAPS_RELAY_TOKEN` | （空＝なし） | 旧の全員で 1 つの合言葉（中継）。客の側では認証情報のファイルより優先する上書き。★通常はアカウントを使う（`laps/relayauth.py`）。 | `laps/relay.py:1601` | 4 |
| `LAPS_RELAY_DEVICE_KEY` | （空＝なし） | 中継へ名乗る機械の鍵。中継と機械（remotehost）の両方に同じ値を入れると、鍵の無い機械は名乗れない。 | `laps/relay.py:1602` | 1 |
| `LAPS_RELAY_AUTH` | optional | 中継のアカウント。`off`＝認証なし／`optional`＝切り替えの期間（サインインしていない相手にも今までどおり見せる）／`required`＝サインインが必須で、自分の機械だけ見える。 | `laps/relay.py:1603` | 1 |
| `LAPS_RELAY_SIGNUP` | first | 既定は最初の 1 人だけ登録できる。`open` で開く・`closed` で閉じる（中継）。 | `laps/relay.py:1607` | 1 |
| `LAPS_RELAY_TLS_CERT` | （state の tls.crt） | 中継の TLS の証明書。証明書と鍵が揃っていれば同じ口で TLS も受ける。 | `laps/relay.py:1611` | 1 |
| `LAPS_RELAY_TLS_KEY` | （state の tls.key） | 中継の TLS の秘密鍵。 | `laps/relay.py:1612` | 1 |
| `LAPS_RELAY_CA` | （同梱の relay_cert.pem） | 客が信頼する中継の証明書のファイル（別の中継や試験で使う）。 | `laps/relayauth.py:116` | 1 |
| `LAPS_AUTH_FILE` | ~/.config/laps/auth.json（旧い ~/.config/blackwall が在ればその中） | 認証情報の置き場（客）。 | `laps/relayauth.py:55` | 1 |
| `LAPS_RELAY_METRICS` | `http://127.0.0.1:8742/v1/metrics` | 中継が `/server/metrics` で返す推論機の機械の数え（bw-metrics）の場所。Android とウェブ版の GPU のグラフが読む（2026-10-18）。 | `laps/relay.py:84` | 1 |
| `LAPS_RELAY_HOST` | 0.0.0.0 | 中継（relay.py）の待ち受けの住所。 | `laps/relay.py:1591` | 1 |
| `LAPS_RELAY_PORT` | 18400 | 中継（relay.py）の待ち受けのポート。 | `laps/relay.py:1592` | 1 |
| `LAPS_RELAY_STATE` | ~/.local/share/laps/relay | 中継が見た機械の一覧（devices.json）とアカウントを置く所。 | `laps/relay.py:1593` | 1 |
| `LAPS_REMOTE` | （未設定＝名乗る） | `0` で、この機械を中継に名乗らせない（remotehost がすぐ終わる・Windows のアプリも起こさない）。設定 `remote_control = false` と同じ。 | `laps/remotehost.py:458` | 1 |
| `LAPS_REMOTE_ATTACH` | （空） | 既にあるゲートウェイ（例 http://127.0.0.1:18400）をそのまま中継に名乗らせる。空なら remotehost が自分の中に 127.0.0.1 だけのゲートウェイを立てる。 | `laps/remotehost.py:462` | 1 |
| `LAPS_REMOTE_KIND` | windows／linux | 機械の一覧に出す種類（windows・linux・gateway）。 | `laps/relayauth.py:334` | 1 |
| `LAPS_RUN_VIA` | （なし） | この本体を起こした側（アプリは `app`・ゲートウェイは `gateway`）。走行の記録と会話の目録に載り、同じ機械のアプリがスマホから直接走った会話を見分けて実況で追う。利用者が設定する物ではない。 | `laps/engine/server.py:582` | 1 |
| `LAPS_REMOTE_MAX` | 4 | スマホから同時に走らせる本体（serve）の数の上限（この機械の中のゲートウェイ）。 | `laps/remotehost.py:192` | 1 |
| `LAPS_REMOTE_WORKSPACE` | （空） | スマホからの走行の作業フォルダ（LAPS_REMOTE_WSFILE が無いとき・remotehost）。機械の操作の線（remotectl）の添付の置き場も、これがあれば機械の今のフォルダとして使う。 | `laps/remotectl.py:202` | 1 |
| `LAPS_REMOTE_WSFILE` | （Windows のアプリ: <データ根>\\workspace.json） | スマホからの走行の作業フォルダを、この JSON の `current` から走行ごとに読み直す。無ければ LAPS_REMOTE_WORKSPACE・LAPS_WORKSPACE・ホーム。 | `laps/remotectl.py:202` | 1 |
| `LAPS_PARENT_PID` | （未設定） | remotehost の親（Windows のアプリ）の PID。親が終わったら 5 秒以内に remotehost も終わる。 | `laps/remotehost.py:165` | 1 |
| `LAPS_APP_VERSION` | （Windows のアプリが渡す） | remotehost が中継の機械一覧に出す版。同梱の Python では本体の版が取れない（0.0.0+dev になった）ので、アプリが自分の版を渡す。 | `laps/gateway.py:100` | 1 |
| `LAPS_INSTALL_PYRUNTIME` | （Windows のアプリが渡す） | 整合性の確認（laps/integrity.py）で、インストール先の同梱 Python の置き場。動かしている写しと別なら両方を照合値（INTEGRITY.json）と突き合わせ、「直す」では写しの壊れたファイルだけをここから写し直す。 | `laps/integrity.py:85` | 1 |
| `LAPS_GATEWAY_WORKSPACE` | /root/bw-mobile | ゲートウェイ経由の既定 workspace。 | `laps/gateway.py:876` | 1 |
| `LAPS_HEARTBEAT_SECS` | 15 | serve の心拍（生存通知）間隔秒。 | `laps/engine/server.py:724` | 1 |
| `LAPS_TOKEN_COALESCE_CHARS` | 400（0 で無効） | 長走行で events.jsonl が肥大しないよう token 事象の**記録**を N 文字ごとに間引く（画面と gateway 生中継はチャンクのまま） | `laps/engine/session.py:75` | 1 |
| `LAPS_SERVE_MAX_RESULT` | (実装既定値) | serve の JSONL に載せる 1 件あたりの結果本文の上限文字数。 | `laps/engine/loop.py:37` | 1 |
| `LAPS_SERVE_WORKSPACE_ROOT` | (空=制限なし) | serve が接続元から受け取る workspace 指定を、このディレクトリ配下に強制する。workspace = policy の封じ込め境界そのものなので、自由選択を許すと workspace モードの意味が消える、という理由つき。 | `laps/engine/server.py:125` | 1 |
| `LAPS_TOOLSET` | full | 登録する道具の集合。`focused` で6本＋opt-in（SystemState 既定込み）に絞る。gateway/serve 双方の既定は `full`。 | `laps/engine/server.py:115` | 5 |
| `LAPS_APPLYDIFF` | 未設定（既定で ApplyDiff を full 道具表から除外） | `1` で ApplyDiff を道具として復元（生涯1回・Edit の失敗経路の翻訳器としては常に生きている・測定用の逃げ道） | `laps/tools/__init__.py:371` | 1 |
| `LAPS_TASK_TOOLS` | 0 | 1 で TaskCreate/TaskUpdate/TaskList/TaskGet を TodoWrite と並べて出す（既定は TodoWrite だけ＝ハーネスが読む帳簿に一本化）。 | `laps/tools/__init__.py:376` | 1 |

## 端末クライアント・UI（term/**）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_METRICS_URL` | (未設定→推論先と同じ機械の `:8742/v1/metrics`) | 全画面表示（tui）の server の箱が読む推論機の測定（bw-metrics）の URL。 | `laps/term/tui.py:485` | 1 |
| `LAPS_MODELCTL_URL` | (未設定→推論先と同じ機械の `:8743/v1/models`) | 全画面表示（tui）が載っているモデル名・電力の上限・周波数を読む モデル切り替えの口 の URL。 | `laps/remotectl.py:572` | 1 |
| `LAPS_TERM_DEBUG` | (未設定) | 端末クライアントのデバッグログ出力先。`off`/`0`/`no`/空で無効。既定の置き場所`~/.blackwall/term-debug.log`。 | `laps/term/app.py:1105` | 1 |
| `LAPS_UI` | tui | 端末の画面。`tui` = btop 風の全画面（対話のときだけ）／`classic` = 従来の行の形。設定ファイルの `ui`・`--ui` と同じ。2026-10-12 オーナー「TUI作りこんでほしいです。Btopみたいに」。 | `laps/term/config.py:362` | 1 |
| `LAPS_WAVE` | (未設定→設定ファイルの wave か None) | 端末の波エフェクトの出し方。語彙外の値は警告つきで無視。優先順位: 環境変数 > 設定ファイル `wave` > 呼び手既定（`--calm`/NO_COLOR）。 | `laps/term/config.py:222` | 3 |

## その他

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
| `LAPS_ASSUME_ONLINE` | (空) | ネットワーク到達性チェックを飛ばして強制的にオンライン/オフライン扱いにする（テスト用・2秒の実測を待たない）。 | `laps/envpack.py:263` | 1 |
| `LAPS_AUTOLINT` | 1 | 編集（Write・Edit）の後に、そのファイルだけへプロジェクト自身の速い検査（ruff・eslint・php -l）を 1 回当て、誤りがあれば `<diagnostics>` に足す。★プロジェクトの外（使い捨ての台本）では走らせない。1 走行 40 回まで・1 回 20 秒まで。`0` で切る。2026-10-17 オーナー「開発機能を最大限まで強化したい」。 | `laps/oc/tools.py:354` | 1 |
| `LAPS_AUTOVERIFY` | 1 | コードを書き換えたまま確かめずに終えようとしたら、ハーネスが Check の速い検査（linter・型）を 1 回走らせる。通れば確かめた扱いで終わり、落ちたら失敗の一覧を渡して差し戻す（1 走行 1 回・試験は走らせない）。`0` で切る。 | `laps/engine/finish.py:193` | 1 |
| `LAPS_DIFFREVIEW` | 1 | 終わる前に、この走行で足した行を 1 回見直す（衝突の印・デバッグ出力・秘密らしい値・試験の絞り込み・build 成果物）。衝突の印と秘密だけ差し戻す。`0` で切る。 | `laps/oc/diffreview.py:117` | 1 |
| `LAPS_REPOMAP` | 1 | system prompt に軽量リポジトリマップを含めるか。作業場所が変わっても控えを持たない設計なので毎回作り直す。 | `laps/oc/prompt.py:453` | 1 |
| `LAPS_SANDBOX` | (未設定→自動検出) | サンドボックスモードの明示指定（`sandbox.py`）。 | `laps/sandbox.py:52` | 1 |
| `LAPS_CHECKPOINT_MAX_EXCLUDED_FILES` | (統合で判明・要記述) | (統合で判明・要記述) | `laps/checkpoint.py:150` | 1 |
| `LAPS_STALL_DUMP_SECS` | 90 | 走行中に事象が出ない沈黙が N 秒続いたら、serve が全スレッドのスタックを stderr（sidecar.log）と LAPS_HOME/stall-*.txt に自分で書く（1 走行 1 回）。実機の「CPU 0・通信 0 の沈黙」の待ち先を次の再発で読むため。 | `laps/engine/server.py:725` | 1 |
| `LAPS_PROCRUN_MAX_BYTES` | 8388608 | procrun.run（固まらない subprocess）が一時ファイルから読み戻す stdout/stderr の上限バイト。Windows の読取スレッド join で永久停止した git check-ignore の再発防止で導入。 | `laps/procrun.py:26` | 1 |
| `LAPS_BASH_TIMEOUT_WIN` | 60 | Windows での Bash 道具の既定 timeout（秒）。Linux は 120 のまま。実機で `dir /s` 等が 120 秒まで沈黙して「止まった」に見えたため短縮。 | `laps/tools/bash.py:4174` | 1 |
| `LAPS_GATEWAY_TELEMETRY_URL` | (空＝送らない) | gateway 経由（Web/Android）の走行を実機と同じ形の record として POST する先。空文字で送らない。Windows 版の Send log と同じ可視性を Web 経路にも持たせるため。 | `laps/gateway_record.py:28` | 1 |
| `LAPS_OUT_QUEUE` | 5000 | serve の stdout 書き出し待ち行列の上限（行）。読み手（アプリ/gateway）が止まっても agent が凍らないよう、書き出しは専用スレッド。満杯なら token を捨てる。 | `laps/engine/wire.py:55` | 1 |
| `LAPS_OUT_BLOCK_SECS` | 2 | 待ち行列が満杯のとき token 以外の事象を待つ最大秒数。超えたら捨てて stderr に 1 行。 | `laps/engine/wire.py:91` | 1 |
| `LAPS_OUT_SYNC` | 0 | 1 で serve の stdout 書き出しを同期に戻す（専用スレッドを使わない）。メモリ上の stream（試験）は常に同期。 | `laps/engine/wire.py:54` | 1 |

## 既知の食い違い（要修正候補・今回は報告のみ）

- **`LAPS_POLICY` の既定解釈が2通りある。** `policy._mode()` / `envpack.py` / `gateway.py` は未設定を **`trusted`** として扱う（2026-08-29 オーナー指示の既定変更）。ところが `laps/tools/bash.py:717` は sudo のヒント文を出すかどうかを `os.environ.get("LAPS_POLICY", "").strip().lower() == "trusted"` で **別実装**しており、既定 `""` を使っている。未設定（＝実際には trusted）のとき `"" == "trusted"` は False になるので、**trusted が既定の出荷構成では このヒント文が出ない**——`policy.py` 側の許可判定は正しく trusted のまま動くので セキュリティ上の穴ではないが、`_mode()` を再実装せず import して使うべき箇所。 `laps/tools/bash.py` はこの担当の対象外のため、コードは変更せず報告のみ。

- **`LAPS_ETHICS`（倫理の刻み: strict/standard/direct/raw/sham）と`LAPS_POLICY`（実行モード: readonly/workspace/off/trusted）は別々の軸。** 「trusted / workspace / raw」のように両者を1本の並びとして書くと誤り——`raw` は POLICY の値になったことがない（ethics.py の`LEVELS`/`MEASUREMENT_LEVELS` にしか無い）。詳細は `docs/policy-modes.md`。

## 画像制作（laps/imaging・2026-11-24）

| env var | 既定 | 意味 | 最初の参照 (file:line) | 出現数 |
|---|---|---|---|---|
