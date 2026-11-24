# Web クライアント（ブラウザだけで使う BLACKWALL）— 2026-09-07

`http://<ゲートウェイの住所>:18400/web/`

ゲートウェイ（Android 版と同じ口）が、`laps/webui/` の 3 枚（HTML/CSS/JS）を
`/web/` で配る。**入れる物は何も無い**——ブラウザで開くだけで、開発機 の `serve` が走り、
推論は 推論機 の LAN 推論機が担う。

## 使い方

1. LAN（または WireGuard）から `http://<ゲートウェイの住所>:18400/web/` を開く。
2. 最初に**合言葉**を聞かれる。開発機:/home/user/project/deploy/gateway.token` の中身を貼る。
   - 合言葉はそのブラウザの `localStorage` にだけ残る（`bw.token`）。
   - 消すときは右上の「合言葉を消す」。
   - 違う合言葉は「合言葉が違います」と出る（黙って繋がらない、にはしない）。
3. 下の入力欄に依頼を書いて **Send**（`Ctrl+Enter` でも送れる。`Enter` は改行）。
4. 走っている間は入力欄の上に 1 行だけ状態が出る:
   印 · いま何をしているか（道具名／Writing／Gerund）· 経過 · ↓トークン · 注記。
   - 推論機が黙っていると、注記に「推論機の応答を待っています（N 秒）」が出る。
5. 途中で止めるときは **Stop**（`cancel`）。作業は捨てられる。
6. 本体が聞き返してきたら（AskUserQuestion）、選択肢を押すか、自分の言葉で答える。

## 覚えていること・覚えていないこと

- 会話は**続く**。`done` に載る `session` を次の依頼で名乗るので、前のやり取りを踏まえて答える。
- ブラウザを閉じても走行は 開発機 で続く。開き直すと同じ `session`（`localStorage` の `bw.session`）
  で繋ぎ直し、`since_seq` で**続きから**描く（二重には描かない）。
- 回線が切れたら自動で繋ぎ直す（1→2→4→8→15 秒）。上の帯に理由が出る。
- ゲートウェイの控えは直近 4,000 行。それより前に切れていた分は埋まらない
  （帯に「欠 N」と出る＝黙って無かったことにはしない）。

## 限界（ここは Windows 版アプリの仕事）

- **作業場は 開発機 側のフォルダ**（既定 `/root/bw-mobile`）。
  実機（Windows）の `D:\` や `C:\` は**触れない**。実機のローカルフォルダを扱う課題は、
  Windows 版アプリでしか成立しない。
- 走行は同時 4 本まで（`LAPS_GATEWAY_MAX`）。埋まっていると `/ws` が 503。
- ファイルの投げ込み（ドラッグ&ドロップ）・画像・記録の送信は無い。
- モデルの入れ替えや LAN 推論機の設定は画面から変えられない（gateway の起動時の環境変数で決まる）。

## 仕組み（直すときに読むところ）

| 何 | どこ |
|---|---|
| 静的配信（`/web/`） | `laps/gateway.py` の `Gateway.web_file`（**合言葉の門より前**に置いてある） |
| 事象 → 状態（純関数） | `laps/webui/app.js` の `bwReduce`（Android の `EventReducer.kt` と同じ約束） |
| 状態 → DOM | 同 `bwRender`（同じ item は作り直さない） |
| 線 | 同 `bwConnect` / `bwSendLine`（`run` / `cancel` / `answer`） |
| 煙試験 | `laps/webui/run_smoke.js`（`node run_smoke.js`／`tests/test_webui_smoke.py` 経由で preflight） |
| 配信の試験 | `tests/test_gateway_web_static.py`（生の HTTP で `..` を畳まずに送る） |

### 静的配信に合言葉を求めない理由

HTML を取りに行く時点で 401 にすると、**合言葉を入力する画面そのものが出せない**。
出て行くのは画面のコードだけで、走行の題も本文も含まない。鍵をかける対象は
`/ws`・`/sessions`・`/sessions/<id>/events` のほうで、そちらは従来どおり合言葉が要る。

### 出しているファイル

`WEB_TYPES` の表に載っている拡張子だけを出す（`.html/.js/.css/.svg/.ico/.json/.txt/.woff2`）。
`webui/` に何かが紛れ込んでも（`.py`・`.token`・`.log`）線の向こうへは出ない。
`..`・`%2e%2e`・`//`・symlink は `webui/` の外を指した時点で断る。

## 動かないときに見るところ

| 症状 | 見るもの |
|---|---|
| ページが 404 | `curl -s http://<ゲートウェイの住所>:18400/health` の `"web"`。`false` なら その機械に画面のファイルが無い |
| 合言葉が通らない | 開発機:/home/user/project/deploy/gateway.token`（0600）。`LAPS_GATEWAY_TOKEN` を設定していればそちらが優先 |
| 開いた直後に切れる | `journalctl -u blackwall-gateway -n 50`。`busy: 4 sessions` なら走行が埋まっている |
| 「本体が終わりました」 | serve が落ちた（close 4001）。ゲートウェイのログに `returncode` が出る |
| 何も返らない | 状態行の注記。「推論機の応答を待っています」なら 推論機 の `llama-server` を見る |
