---
name: php-extension-broken
description: PHP の拡張が読み込めない（Unable to load dynamic library / undefined symbol / already loaded）。コードの誤りではなく環境の欠陥なので、編集を続けても検収は通らない。走査先の確認と、拡張を使わない逃げ道。
match: "Unable to load dynamic library|undefined symbol:|Module .{0,40}already loaded|\.so\.so"
on: ["Bash", "BashOutput", "PhaseAcceptance"]
---

## 差し込む

[ハーネスからの覚え書き — 利用者の発言ではありません]
PHP 拡張が**読み込めていません**。これはあなたのコードの誤りではなく**環境の欠陥**で、
site/ の PHP を何度書き直しても検収は通りません。次の順で切り分けてください。

1. `php -m | grep -i -E 'pdo|sqlite'` と `php --ini` — 何が載っていて、どの ini を読むか
2. `undefined symbol: pdo_dbh_ce` は **pdo.so より先に pdo_sqlite.so を読んだ**か、
   **PHP 本体と .so の版が違う**形。ini の `extension=` の順序（pdo を先に）を1度だけ試す
3. `Module "PDO" is already loaded` は**二重登録**。自分が足した `extension=` 行を戻す
4. 2 と 3 で直らなければ**その拡張は諦める**。拡張を使わずに検収を満たす:
   `sqlite3 db "SQL"` を `shell_exec` で呼び JSON を受け取る／事前に JSON を書き出しておく

★次の 1 手番は 1 だけを実行し、出力を見てから 2〜4 を選ぶこと。**編集はまだしない。**

## なぜ（人が読む部分・注入されない）

実測（`scratch/vdirtest/run{29,31,32,34}-trace.jsonl`・2026-08-28〜29）:

- run32 / run34 は「API と配信」の 3 試行すべてを、`pdo_sqlite.so: undefined symbol: pdo_dbh_ce`
  を抱えたまま走り切って不合格。この文字列は道具の結果に **119 回 / 122 回**現れている。
- run29 は同じ壁に `Module "PDO" is already loaded`（＝拡張を直そうとして ini に
  `extension=pdo` を足し、二重登録になった形）で **64 回**。
- 4 走行とも、詰まっていたのは HTTP Range 206 の書き方ではなく**この拡張**。
  Range 付き curl は run32 で 47 回・run34 で 41 回 206 を返している＝実装はできていた。

★この手順書が「直し方」ではなく「切り分けと諦め方」なのは、4 走行の実測が
  「直そうとして 8〜9 時間溶かした」側だから。**逃げ道（4）が本命。**
