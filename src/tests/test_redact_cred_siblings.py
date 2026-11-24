"""★2026-09-21 友人の実機の記録: `.env` の読み取り結果が記録に乗り、`MT5_PASSWORD` は伏せたが
`MT5_LOGIN`（口座番号）と `MT5_SERVER` が素のまま残った。**同じ接頭辞の秘密と並ぶ識別子**も伏せる。
★値はすべて偽物（実在の値を例に使わない）。"""
import json

from laps.redact import redact

SALT = b"test-salt"
ENV = "# C:\\x\\.env.v4\n1  MT5_LOGIN=900000001\n2  MT5_PASSWORD=Zx9-demo42-Fake!!\n3  MT5_SERVER=DemoBroker-Live07"


def test_秘密と同じ接頭辞の口座番号とサーバ名も伏せる():
    r = redact(ENV, salt=SALT)
    assert "Zx9-demo42" not in r.text and "900000001" not in r.text and "DemoBroker-Live07" not in r.text
    assert "MT5_LOGIN=" in r.text and "MT5_SERVER=" in r.text          # 鍵の名前は残す（読めるように）
    assert r.kinds.get("cred_sibling") == 2


def test_JSONの一行に入った形でも効く():
    """送信前の濾し器は JSON の 1 行に掛かる（改行は \\n・引用符は \\" に逃がされている）。"""
    line = json.dumps({"tools": [{"result_head": ENV}]}, ensure_ascii=False)
    r = redact(line, salt=SALT)
    assert "900000001" not in r.text and "DemoBroker-Live07" not in r.text
    json.loads(r.text)                                                  # JSON として壊していない


def test_秘密が無い本文では識別子に触らない():
    """`LOGIN=` をどこでも伏せると誤爆する。秘密を伏せた本文でだけ兄弟を伏せる。"""
    t = "MT5_LOGIN=900000001\nMT5_SERVER=DemoBroker-Live07\nDB_USER=app"
    r = redact(t, salt=SALT)
    assert r.text == t and r.count == 0


def test_接頭辞の違う識別子と_HOST_PORT_には触らない():
    t = "DB_PASSWORD=Zx9-demo42-Fake!!\nDB_USER=appuser\nDB_HOST=192.0.2.5\nDB_PORT=5432\nAPP_USER=alice"
    r = redact(t, salt=SALT)
    assert "appuser" not in r.text                  # DB_ の兄弟
    assert "DB_HOST=192.0.2.5" in r.text and "DB_PORT=5432" in r.text
    assert "APP_USER=alice" in r.text               # 接頭辞が違う


def test_プレースホルダは伏せない():
    t = "MT5_PASSWORD=Zx9-demo42-Fake!!\nMT5_LOGIN=<your-login>"
    assert "<your-login>" in redact(t, salt=SALT).text


def test_既に伏字の印になっている本文でも兄弟を伏せる():
    """保存済みの記録を後から伏せ直す場面。パスワードは印になっていて、兄弟だけが素で残っている。"""
    once = redact("MT5_PASSWORD=Zx9-demo42-Fake!!", salt=SALT).text
    t = f"1  MT5_LOGIN=900000001\n2  {once}\n3  MT5_SERVER=DemoBroker-Live07"
    r = redact(t, salt=SALT)
    assert "900000001" not in r.text and "DemoBroker-Live07" not in r.text
    assert once in r.text                           # 印は二重に伏せない
