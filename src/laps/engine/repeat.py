"""生成中の繰り返しの検知（2026-10-30 オーナー「内部思考でおんなじことを繰り返すことが非常に多い」）。

手本: pi-deadloop（同じ考えを文字の組の重なりで比べる）と、opencode に上がっている思考の暴走の報告
（issue 44962・46249・同じ 1 文が数百回）。サンプラの繰り返しの罰は同じ語の並びしか見ないので、
言い回しを少し変えた堂々巡りには効かない。

見る形は 3 つ:
  1. 同じ文がそのまま何度も出る
  2. ほとんど同じ文が何度も出る（「takina_info.txt を確認します」「takina の元の動画を確認します」）
  3. 文に切れない繰り返し（「!!!!」や同じ断片の周期）
  4. 迷いの書き出しが続く（「しかし、おそらく…」「または、おそらく…」・思考だけ。呼び手が hedge を渡す）
"""
from __future__ import annotations

import re

_SPLIT = re.compile(r"(?<=[。．！？!?])|(?<=\.)\s|\n+")
_WS = re.compile(r"\s+")
#: 迷いの書き出し（2026-10-31 オーナー「内部思考で、しかし、おそらくと言ってずっと考えている」）。
#: 文が毎回少しずつ違うので 1・2 の形には掛からない（最長の思考 84 秒・「おそらく」57 回）。文はすでに小文字。
_HEDGE = re.compile(r"(?:しかし|または|あるいは|おそらく|もしかすると|もしかしたら|たぶん|多分|いや|待って|でも|それとも|ただし|ですが|とはいえ)"
                    r"|(?:however|but|or|actually|wait|alternatively|maybe|perhaps|hmm)\b")


#: コードの行。★思考の中で編集を下書きすると old_string と new_string に同じ行が必ず並ぶので、数えると正しい下書きを切る
#: （2026-10-31 課題集の addflag: 6 回切られて走行が止まった）。コードの堂々巡りは周期の検知と思考の上限が止める。
_CODE_LINE = re.compile(r"^\s{2,}\S|^(?:def |class |import |from |return |if |elif |else:|for |while |with |try:|except|print\(|"
                        r"const |let |var |function |#include|@@|[-+]{1,3}\s)|[;{}]\s*$|\)\s*:?\s*$|^[\w.\[\]'\"]+\s*=\s*\S")


def _grams(s: str) -> frozenset:
    return frozenset(s[i:i + 3] for i in range(max(1, len(s) - 2)))


def similar(a: str, b: str) -> float:
    """文字 3 つ組の重なり（Jaccard）。pi-deadloop は 0.85 を使う。"""
    ga, gb = _grams(a), _grams(b)
    return len(ga & gb) / max(1, len(ga | gb))


class RepeatWatch:
    MIN_LEN = 14
    WINDOW = 40
    NEAR = 0.8

    def __init__(self, exact: int = 3, near: int = 5, min_chars: int = 400, hedge: int = 0) -> None:
        self.exact = exact
        self.near = near
        self.min_chars = min_chars           # 短い出力では判定しない（箇条書きの似た行で切らない）
        self.hedge = hedge                   # 直近 WINDOW 文のうち迷いの書き出しがこの数に達したら切る（0 で見ない）
        self.heads: list[bool] = []
        self.in_fence = False                # ``` の中（コードは数えない）
        self.kind = "repeat"                 # 切った理由（repeat / hedge）
        self.buf = ""
        self.total = 0
        self.since_tail = 0
        self.units: list[tuple[str, frozenset]] = []
        self.counts: dict[str, int] = {}

    def feed(self, text: str) -> str | None:
        """断片を足す。繰り返しと判定したらその文を返す（以後は呼び手が生成を切る）。"""
        self.kind = "repeat"
        self.total += len(text)
        self.buf += text
        self.since_tail += len(text)
        hit = self._units()
        if hit is None and self.since_tail >= 256:
            self.since_tail = 0
            hit = self._periodic()
        if hit is not None and self.total >= self.min_chars:
            return hit
        return None

    def _units(self) -> str | None:
        parts = _SPLIT.split(self.buf)
        if len(parts) < 2:
            if len(self.buf) > 8192:
                self.buf = self.buf[-4096:]
            return None
        self.buf = parts[-1] or ""
        for raw in parts[:-1]:
            if raw is None:
                continue
            if raw.lstrip().startswith("```"):
                self.in_fence = not self.in_fence
                continue
            if self.in_fence or _CODE_LINE.search(raw):
                continue
            u = _WS.sub(" ", raw).strip().lower()
            if len(u) < self.MIN_LEN:
                continue
            n = self.counts.get(u, 0) + 1
            self.counts[u] = n
            if n >= self.exact:
                return u
            g = _grams(u)
            close = sum(1 for _, o in self.units if len(g & o) / max(1, len(g | o)) >= self.NEAR)
            self.units.append((u, g))
            del self.units[:-self.WINDOW]
            if close + 1 >= self.near:
                return u
            if self.hedge:
                self.heads.append(bool(_HEDGE.match(u)))
                del self.heads[:-self.WINDOW]
                if sum(self.heads) >= self.hedge:
                    self.kind = "hedge"
                    return u
        return None

    def _periodic(self) -> str | None:
        """同じ断片が 4 回続く（文の切れ目が無い形）。"""
        s = self.buf[-2400:]
        if len(s) < 200:
            return None
        for p in range(1, 400):
            if len(s) < p * 4:
                break
            tail = s[-p:]
            if s[-2 * p:-p] == tail and s[-3 * p:-2 * p] == tail and s[-4 * p:-3 * p] == tail:
                return tail.strip()[:80] or tail[:80]
        return None
