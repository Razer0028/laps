"""QR コードを作る（標準ライブラリだけ・2026-10-31）。

オーナー「リモートコントロールの機能を Claude と同じ感じに」。Claude の /remote-control は会話の住所と QR を出し、スマホで読むと
その会話が開く。ここは Windows のアプリと端末版の両方が使う（同じ物を 2 つの言語で書かない・アプリは relayauth の op "rc" 経由）。

バイトのモード・誤り訂正 L だけ（住所を載せるだけなので。L は同じ大きさで載る量がいちばん多い）。
手順は ISO/IEC 18004 のとおり: 符号語を作る → ブロックに分けて訂正符号を足す → 交互に並べる → 図形を置く →
8 通りのマスクから減点が最小の物を選ぶ。

    rows = encode("https://example.com")   # 1 行 1 文字列（"1"＝黒・"0"＝白）。周りの余白は含まない
    text = halfblocks(rows)                # 端末に出す形（上下半分のブロック文字）
"""
from __future__ import annotations

# バージョンごとの 1 ブロックの訂正符号語の数とブロックの数（誤り訂正 L・添字がバージョン）
_ECC_PER_BLOCK = (-1, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18, 20, 24, 26, 30, 22, 24, 28, 30, 28, 28,
                  28, 28, 30, 30, 26, 28, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30, 30)
_BLOCKS = (-1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4, 4, 4, 4, 4, 6, 6, 6, 6, 7, 8,
           8, 9, 9, 10, 12, 12, 12, 13, 14, 15, 16, 17, 18, 19, 19, 20, 21, 22, 24, 25)
_FORMAT_L = 1            # 形式情報の誤り訂正の印（L は 01）
_PENALTY = (3, 3, 40, 10)


def _raw_modules(ver: int) -> int:
    n = (16 * ver + 128) * ver + 64
    if ver >= 2:
        align = ver // 7 + 2
        n -= (25 * align - 10) * align - 55
        if ver >= 7:
            n -= 36
    return n


def _capacity(ver: int) -> int:
    return _raw_modules(ver) // 8 - _ECC_PER_BLOCK[ver] * _BLOCKS[ver]


def _gf_mul(x: int, y: int) -> int:
    z = 0
    for i in range(7, -1, -1):
        z = (z << 1) ^ ((z >> 7) * 0x11D)
        z ^= ((y >> i) & 1) * x
    return z


def _rs_divisor(degree: int) -> list[int]:
    res = [0] * (degree - 1) + [1]
    root = 1
    for _ in range(degree):
        for j in range(degree):
            res[j] = _gf_mul(res[j], root)
            if j + 1 < degree:
                res[j] ^= res[j + 1]
        root = _gf_mul(root, 2)
    return res


def _rs_remainder(data: list[int], divisor: list[int]) -> list[int]:
    res = [0] * len(divisor)
    for b in data:
        factor = b ^ res.pop(0)
        res.append(0)
        for i, coef in enumerate(divisor):
            res[i] ^= _gf_mul(coef, factor)
    return res


def _codewords(data: bytes, ver: int) -> list[int]:
    """データの符号語を作り、ブロックに分けて訂正符号を足し、交互に並べる。"""
    cap = _capacity(ver)
    bits: list[int] = []

    def put(val: int, n: int) -> None:
        bits.extend((val >> i) & 1 for i in range(n - 1, -1, -1))

    put(0b0100, 4)
    put(len(data), 8 if ver <= 9 else 16)
    for b in data:
        put(b, 8)
    put(0, min(4, cap * 8 - len(bits)))
    put(0, -len(bits) % 8)
    words = [int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8)]
    pad = 0xEC
    while len(words) < cap:
        words.append(pad)
        pad ^= 0xEC ^ 0x11

    blocks_n, ecc_n = _BLOCKS[ver], _ECC_PER_BLOCK[ver]
    raw = _raw_modules(ver) // 8
    short_n = blocks_n - raw % blocks_n
    short_len = raw // blocks_n
    div = _rs_divisor(ecc_n)
    blocks: list[list[int]] = []
    k = 0
    for i in range(blocks_n):
        n = short_len - ecc_n + (0 if i < short_n else 1)
        dat = words[k:k + n]
        k += n
        ecc = _rs_remainder(dat, div)
        if i < short_n:
            dat = dat + [0]        # 短いブロックは列を揃えるための穴（並べるときに飛ばす）
        blocks.append(dat + ecc)
    out: list[int] = []
    for col in range(len(blocks[0])):
        for j, block in enumerate(blocks):
            if col != short_len - ecc_n or j >= short_n:
                out.append(block[col])
    return out


def _align_positions(ver: int) -> list[int]:
    if ver == 1:
        return []
    n = ver // 7 + 2
    step = 26 if ver == 32 else (ver * 4 + n * 2 + 1) // (n * 2 - 2) * 2
    size = ver * 4 + 17
    res = [6]
    pos = size - 7
    tail = []
    for _ in range(n - 1):
        tail.append(pos)
        pos -= step
    return res + tail[::-1]


class _Grid:
    def __init__(self, ver: int) -> None:
        self.ver = ver
        self.size = ver * 4 + 17
        self.mod = [[False] * self.size for _ in range(self.size)]
        self.fun = [[False] * self.size for _ in range(self.size)]

    def set_fun(self, x: int, y: int, black: bool) -> None:
        self.mod[y][x] = black
        self.fun[y][x] = True

    def draw_patterns(self) -> None:
        s = self.size
        for i in range(s):
            self.set_fun(6, i, i % 2 == 0)
            self.set_fun(i, 6, i % 2 == 0)
        for cx, cy in ((3, 3), (s - 4, 3), (3, s - 4)):
            for dy in range(-4, 5):
                for dx in range(-4, 5):
                    x, y = cx + dx, cy + dy
                    if 0 <= x < s and 0 <= y < s:
                        self.set_fun(x, y, max(abs(dx), abs(dy)) not in (2, 4))
        pos = _align_positions(self.ver)
        for i, cx in enumerate(pos):
            for j, cy in enumerate(pos):
                if (i == 0 and j == 0) or (i == 0 and j == len(pos) - 1) or (i == len(pos) - 1 and j == 0):
                    continue
                for dy in range(-2, 3):
                    for dx in range(-2, 3):
                        self.set_fun(cx + dx, cy + dy, max(abs(dx), abs(dy)) != 1)
        self.draw_format(0)
        if self.ver >= 7:
            rem = self.ver
            for _ in range(12):
                rem = (rem << 1) ^ ((rem >> 11) * 0x1F25)
            bits = self.ver << 12 | rem
            for i in range(18):
                b = (bits >> i) & 1 == 1
                a, c = s - 11 + i % 3, i // 3
                self.set_fun(a, c, b)
                self.set_fun(c, a, b)

    def draw_format(self, mask: int) -> None:
        data = _FORMAT_L << 3 | mask
        rem = data
        for _ in range(10):
            rem = (rem << 1) ^ ((rem >> 9) * 0x537)
        bits = (data << 10 | rem) ^ 0x5412
        s = self.size
        bit = lambda i: (bits >> i) & 1 == 1  # noqa: E731
        for i in range(6):
            self.set_fun(8, i, bit(i))
        self.set_fun(8, 7, bit(6))
        self.set_fun(8, 8, bit(7))
        self.set_fun(7, 8, bit(8))
        for i in range(9, 15):
            self.set_fun(14 - i, 8, bit(i))
        for i in range(8):
            self.set_fun(s - 1 - i, 8, bit(i))
        for i in range(8, 15):
            self.set_fun(8, s - 15 + i, bit(i))
        self.set_fun(8, s - 8, True)

    def draw_codewords(self, words: list[int]) -> None:
        s = self.size
        i = 0
        right = s - 1
        while right >= 1:
            if right == 6:
                right = 5
            for vert in range(s):
                for j in range(2):
                    x = right - j
                    upward = (right + 1) & 2 == 0
                    y = s - 1 - vert if upward else vert
                    if not self.fun[y][x] and i < len(words) * 8:
                        self.mod[y][x] = (words[i >> 3] >> (7 - (i & 7))) & 1 == 1
                        i += 1
            right -= 2

    def apply_mask(self, mask: int) -> None:
        for y in range(self.size):
            for x in range(self.size):
                if self.fun[y][x]:
                    continue
                inv = {0: (x + y) % 2 == 0, 1: y % 2 == 0, 2: x % 3 == 0, 3: (x + y) % 3 == 0,
                       4: (x // 3 + y // 2) % 2 == 0, 5: x * y % 2 + x * y % 3 == 0,
                       6: (x * y % 2 + x * y % 3) % 2 == 0, 7: ((x + y) % 2 + x * y % 3) % 2 == 0}[mask]
                self.mod[y][x] ^= inv

    def penalty(self) -> int:
        s, m = self.size, self.mod
        score = 0

        def lines():
            yield from m
            for x in range(s):
                yield [m[y][x] for y in range(s)]

        pat1 = [True, False, True, True, True, False, True]
        for line in lines():
            run_c, run = line[0], 1
            for v in line[1:]:
                if v == run_c:
                    run += 1
                else:
                    if run >= 5:
                        score += _PENALTY[0] + run - 5
                    run_c, run = v, 1
            if run >= 5:
                score += _PENALTY[0] + run - 5
            for i in range(s - 6):
                if line[i:i + 7] == pat1:
                    if (i >= 4 and not any(line[i - 4:i])) or (i + 11 <= s and not any(line[i + 7:i + 11])):
                        score += _PENALTY[2]
        for y in range(s - 1):
            for x in range(s - 1):
                if m[y][x] == m[y][x + 1] == m[y + 1][x] == m[y + 1][x + 1]:
                    score += _PENALTY[1]
        black = sum(map(sum, m))
        k = (abs(black * 20 - s * s * 10) + s * s - 1) // (s * s) - 1
        return score + max(k, 0) * _PENALTY[3]


def encode(text: str) -> list[str]:
    """QR の行の並び（"1"＝黒）。載らなければ ValueError。"""
    data = text.encode("utf-8")
    for ver in range(1, 41):
        head = 4 + (8 if ver <= 9 else 16)
        if head + len(data) * 8 <= _capacity(ver) * 8:
            break
    else:
        raise ValueError("長すぎて QR に載りません")
    base = _Grid(ver)
    base.draw_patterns()
    base.draw_codewords(_codewords(data, ver))
    best, best_score = None, None
    for mask in range(8):
        g = _Grid(ver)
        g.mod = [r[:] for r in base.mod]
        g.fun = base.fun
        g.apply_mask(mask)
        g.draw_format(mask)
        sc = g.penalty()
        if best_score is None or sc < best_score:
            best, best_score = g, sc
    return ["".join("1" if v else "0" for v in row) for row in best.mod]


def halfblocks(rows: list[str], quiet: int = 2) -> str:
    """端末に出す形。黒い端末で読めるよう、黒のマスを空白に、白のマスと余白を塗る（上下半分のブロック文字で 2 行を 1 行に）。"""
    n = len(rows)
    g = [[False] * (n + quiet * 2) for _ in range(quiet)]
    g += [[False] * quiet + [c == "1" for c in r] + [False] * quiet for r in rows]
    g += [[False] * (n + quiet * 2) for _ in range(quiet + (n % 2))]
    out = []
    for y in range(0, len(g) - 1, 2):
        line = []
        for top, bottom in zip(g[y], g[y + 1]):
            line.append({(False, False): "█", (False, True): "▀", (True, False): "▄", (True, True): " "}[(top, bottom)])
        out.append("".join(line))
    return "\n".join(out)
