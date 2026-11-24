"""Blackwall の新しい中核（2026-10-30 オーナー「コードを全部作りなおしてみない？」「参考にするのは OSS メインで」）。

手本は opencode（MIT・THIRD_PARTY.md）の走行部分。旧い中核（laps/oc/loop.py ほか）は読まずに書いた。
旧いコードに触るのは 3 か所だけ: 画面との線の形（wire.py）・実行の可否（laps/policy.py をそのまま呼ぶ）・
ログの伏せ字（laps/redact.py をそのまま呼ぶ）。音声や画面操作などの機能の道具は tools/ext.py から呼ぶ。

組み立て:
    server.py   stdin/stdout の線（run・cancel・inject・answer・app_reply）
    loop.py     1 走行（読む→生成→道具→…→道具を呼ばない手番で終わる）
    llm.py      推論機（OpenAI 互換のストリーム・llama.cpp の拡張）
    repeat.py   生成中の繰り返しの検知
    compact.py  文脈があふれたときの処理
    endgate.py  終わる手番の確かめ（状態で見る。言い回しの差し戻しは持たない）
    prompt.py   前置き
    session.py  会話の記録（messages.jsonl・events.jsonl・meta.json）
    tools/      道具
"""
