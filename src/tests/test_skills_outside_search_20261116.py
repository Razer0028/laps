"""インターネットのスキル探しを強く（2026-11-16 オーナー「インターネット上の Skill 探索と選択に関してより強力なものに。
選択肢の幅が広がるようにして、より最適なものを選べるようにする」）。

何通りかの言い方を並べて探してまとめる・当たった言い方の数と名前で並べる・上位に「何のための物か」を付ける・
SKILL.md の置き場所を GitHub の木から探す・何個かまとめて読む。ネットには出ない（urlopen と探す口を差し替える）。
"""
from __future__ import annotations

from laps.oc import skilltool as S


class _Resp:
    def __init__(self, b: bytes):
        self.b = b

    def read(self, n=-1):
        return self.b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_several_phrasings_are_merged_and_ranked(monkeypatch):
    data = {
        "unity modding": [{"id": "a/x/unity-modding", "source": "a/x", "skillId": "unity-modding", "installs": 10},
                          {"id": "b/y/popular", "source": "b/y", "skillId": "popular", "installs": 9000}],
        "bepinex plugin": [{"id": "a/x/unity-modding", "source": "a/x", "skillId": "unity-modding", "installs": 10},
                           {"id": "c/z/bepinex-plugin", "source": "c/z", "skillId": "bepinex-plugin", "installs": 50}],
    }
    asked = []
    monkeypatch.setattr(S, "_outside_search", lambda q, limit=50: (asked.append(q), (data.get(q, []), ""))[1])
    monkeypatch.setattr(S, "_describe", lambda rows: {"a/x/unity-modding": "Modding Unity games with loaders."})
    out = "\n".join(S._outside_block("unity modding | bepinex plugin"))
    assert asked[:2] == ["unity modding", "bepinex plugin"]
    # 2 つの言い方の両方に当たった物が先（導入数の多い物より上）
    assert out.index("a/x@unity-modding") < out.index("b/y@popular")
    assert "matched 2 searches" in out and "Modding Unity games with loaders." in out
    assert "(no description)" in out                    # 説明が取れなかった物もそうと書く


def test_few_results_widen_with_single_words(monkeypatch):
    asked = []
    monkeypatch.setattr(S, "_outside_search", lambda q, limit=50: (asked.append(q), ([], ""))[1])
    monkeypatch.setattr(S, "_describe", lambda rows: {})
    S._outside_block("harmony patching")
    assert asked[0] == "harmony patching" and "harmony" in asked and "patching" in asked


def test_description_comes_from_the_page_meta(monkeypatch):
    page = b'<html><head><meta name="description" content="Use when building a BepInEx mod.\\nPROACTIVELY activate for: (1)&amp;more"/></head></html>'
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _Resp(page))
    d = S._fetch_desc("a/x/unity-modding")
    assert d.startswith("Use when building a BepInEx mod.") and "&amp;" not in d and "\\n" not in d


def test_read_finds_a_deep_skill_path_from_the_tree(monkeypatch):
    monkeypatch.setattr(S, "_gh_tree", lambda o, r: ["plugins/unity-master/skills/unity-modding/SKILL.md", "README.md"])
    seen = []

    def urlopen(req, timeout=0):
        url = req.full_url
        seen.append(url)
        if url.endswith("/HEAD/plugins/unity-master/skills/unity-modding/SKILL.md"):
            return _Resp(b"---\nname: unity-modding\n---\n# Unity modding\nUse BepInEx.")
        raise OSError("404")
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    r = S._outside_read("o/marketplace@unity-modding")
    assert r.ok and "plugins/unity-master/skills/unity-modding/SKILL.md" in str(r) and "Use BepInEx." in str(r)
    assert "Use it as guidance for this task" in str(r)
    assert seen[0].endswith("/HEAD/plugins/unity-master/skills/unity-modding/SKILL.md")   # 木の場所を最初に試す


def test_read_several_at_once_with_a_smaller_cap(monkeypatch):
    monkeypatch.setattr(S, "_gh_tree", lambda o, r: [])
    big = ("x" * 9000).encode()
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=0: _Resp(b"# Skill\n" + big))
    r = S.skill_tool({"action": "read", "name": "a/b@one, c/d@two"})
    s = str(r)
    assert r.ok and s.count("<outside_skill>") == 2 and "a/b@one" in s and "c/d@two" in s
    assert "read this one alone for the rest" in s
