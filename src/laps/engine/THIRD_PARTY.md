# 手本にした OSS

- opencode (https://github.com/anomalyco/opencode) — MIT License, Copyright (c) 2025 opencode
  走行の流れ・文脈の処理・編集の照合（tools/edit_match.py は replacer の移植）・道具の説明の形。
- Codex CLI (https://github.com/openai/codex) — Apache License 2.0, Copyright 2025 OpenAI
  2026-11-11 に commit d9a32c1 を読んで写した作り: ApplyPatch の差分の形と当て方（tools/patch.py・apply-patch crate の
  parser と seek_sequence）、Edit の字形をそろえる段（tools/edit_match.py の punct_normalized）、命令へ渡す環境の除外と
  NO_COLOR/TERM（tools/shell.py・shell_environment.rs と unified_exec）、走っている命令への入力（Bash の stdin・BashOutput の
  send・write_stdin）、囲い（tools/shellbox.py・linux-sandbox と denial.rs）、要約で利用者の発言を残す（compact.py）、
  止められた依頼の印（loop.py・turn_aborted.rs）、依頼ごとの変更のまとめ（undo.py・turn_diff_tracker.rs）、
  決まりの例の試験（policy.py の EXAMPLES・execpolicy の match / not_match）。コードは Python で書き直した（そのまま写した物は無い）。
