"""3D の網目の点検の数（Read の構造の結果に足す・2026-10-15）。

vrcmodel（VRChat の衣装づくりのセッション）と決めた中身。外から来たモデルに服を着せて台本が崩れた件のうち、
「布が裂ける」「骨と体がずれる」は、ファイルを読むだけで先に分かった、という実測から:
  網目      三角形の数・島（辺でつながる面の塊）・縁の辺（面が 1 枚だけの辺）・同じ位置の頂点（0.01mm 以内）・
            どの面にも使われていない頂点・面積がほぼ 0 の面。
            ★島と縁は「結合前の網目」で数え、同じ位置の頂点をつないだ後の数を並べる。UV の切れ目ごとに面が切れたモデル
            （ゲームから取り出した体で島 57・縁 3,834）は、つなぐと島が数個に減る。この差が「布が裂ける」の正体
            （vrcmodel の実測・作者の体は島 1・縁 20）。
  骨と重み  骨ごとに、重みの付いた頂点の中心（重み付き平均）と骨の位置を比べる。大きさが何十倍も違えば単位の食い違い、
            高さの軸が違えば向きの食い違い（骨は Y が上・体は Z が上）。
  VRChat    評価の見積り（三角形・スキンのメッシュ・普通のメッシュ・マテリアルの枠・骨）。表は creators.vrchat.com の
            Performance Ranks（2026-10-15 に確かめた値）。ファイルから分かる分だけ（揺れもの・テクスチャのメモリ・
            ライト・パーティクルは Unity に入れないと分からない）。
数え方は vrcmodel の点検台本（Blender の bmesh: BMEdge.is_boundary・remove_doubles の dist=1e-5）と同じ。
距離はメートルに直して比べる（ファイルの 1 単位が何 m かは呼ぶ側が渡す）。読むだけ。
"""
from __future__ import annotations

import math

SAME_M = 1e-5                 # 同じ位置とみなす距離（m）＝ remove_doubles の dist
DEGEN_M2 = 1e-9               # 面積ほぼ 0（m²）
MAX_CORNERS = 12_000_000      # 面の角の総数がこれを超える網目は数えない（純 Python で数十秒を超える）


# ── 網目 ─────────────────────────────────────────────────────────────────────

def _edges_islands(polys, nv: int, rep) -> tuple[int, int]:
    """縁の辺（面がちょうど 1 枚の辺）の数と島（辺を共有する面のつながり）の数。rep があれば頂点を付け替えてから数える。"""
    nf = len(polys)
    parent = list(range(nf))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    first: dict[int, int] = {}
    multi: set[int] = set()
    for fi, p in enumerate(polys):
        n = len(p)
        for k in range(n):
            a = p[k]
            b = p[k + 1] if k + 1 < n else p[0]
            if rep is not None:
                a, b = rep[a], rep[b]
            if a == b:
                continue
            key = a * nv + b if a < b else b * nv + a
            f0 = first.get(key)
            if f0 is None:
                first[key] = fi
            else:
                multi.add(key)
                ra, rb = find(fi), find(f0)
                if ra != rb:
                    parent[ra] = rb
    return len(first) - len(multi), sum(1 for i in range(nf) if find(i) == i)


def _same_pos(pos, nv: int, d: float) -> tuple[list[int], int]:
    """d 以内の頂点を 1 つにまとめる（先に来た頂点を代表にする）。代表の表と、まとめた後の頂点の数。
    升の大きさを 2d にすると、各軸で自分の升と近い側の隣だけ見れば足りる（8 升）。"""
    rep = list(range(nv))
    if not d > 0:
        return rep, nv
    inv, d2 = 1.0 / (2 * d), d * d
    grid: dict[tuple[int, int, int], list[int]] = {}
    kept = 0
    for i in range(nv):
        x, y, z = pos[3 * i], pos[3 * i + 1], pos[3 * i + 2]
        try:
            fx, fy, fz = x * inv, y * inv, z * inv
            cx, cy, cz = math.floor(fx), math.floor(fy), math.floor(fz)
        except (ValueError, OverflowError):                # NaN・無限大はほかとまとめない
            kept += 1
            continue
        ox = cx - 1 if fx - cx < 0.5 else cx + 1
        oy = cy - 1 if fy - cy < 0.5 else cy + 1
        oz = cz - 1 if fz - cz < 0.5 else cz + 1
        found = -1
        for gx in (cx, ox):
            for gy in (cy, oy):
                for gz in (cz, oz):
                    for j in grid.get((gx, gy, gz), ()):
                        dx, dy, dz = pos[3 * j] - x, pos[3 * j + 1] - y, pos[3 * j + 2] - z
                        if dx * dx + dy * dy + dz * dz <= d2:
                            found = j
                            break
                    if found >= 0:
                        break
                if found >= 0:
                    break
            if found >= 0:
                break
        if found < 0:
            grid.setdefault((cx, cy, cz), []).append(i)
            kept += 1
        else:
            rep[i] = found
    return rep, kept


def topology(pos, polys, unit_m: float = 1.0) -> dict:
    """pos: [x0, y0, z0, x1, …]（ファイルの単位）・polys: 面ごとの頂点の番号の列・unit_m: ファイルの 1 単位が何 m か。"""
    nv = len(pos) // 3
    corners = sum(len(p) for p in polys)
    if corners > MAX_CORNERS:
        return {"skipped": f"大きすぎるので数えない（面の角 {corners:,}・上限 {MAX_CORNERS:,}）"}
    used = bytearray(nv)
    degen = 0
    lim = DEGEN_M2 / (unit_m * unit_m) if unit_m > 0 else 0.0
    for p in polys:
        for v in p:
            used[v] = 1
        # ニューエルの方法で面積（平らでない多角形でもおおよそ）
        sx = sy = sz = 0.0
        n = len(p)
        for k in range(n):
            a, b = p[k], p[k + 1] if k + 1 < n else p[0]
            ax, ay, az = pos[3 * a], pos[3 * a + 1], pos[3 * a + 2]
            bx, by, bz = pos[3 * b], pos[3 * b + 1], pos[3 * b + 2]
            sx += ay * bz - az * by
            sy += az * bx - ax * bz
            sz += ax * by - ay * bx
        if 0.5 * math.sqrt(sx * sx + sy * sy + sz * sz) < lim:
            degen += 1
    bd, isl = _edges_islands(polys, nv, None)
    rep, kept = _same_pos(pos, nv, SAME_M / unit_m if unit_m > 0 else 0.0)
    same = nv - kept
    bd_m, isl_m = _edges_islands(polys, nv, rep) if same else (bd, isl)
    return {"verts": nv, "faces": len(polys), "tris": sum(max(len(p) - 2, 0) for p in polys),
            "boundary": bd, "islands": isl, "same": same, "boundary_m": bd_m, "islands_m": isl_m,
            "loose": nv - sum(used), "degenerate": degen}


def topo_line(t: dict) -> str:
    """1 つの網目の数を 1 行に。★は vrcmodel の合格線（退化面 0・切れ端の頂点 0）と、面が切れている形。"""
    if "skipped" in t:
        return t["skipped"]
    s = f"三角形 {t['tris']:,}・島 {t['islands']:,}"
    if t["same"] and t["islands_m"] != t["islands"]:
        s += f"（同じ位置をつなぐと {t['islands_m']:,}）"
    s += f"・縁の辺 {t['boundary']:,}"
    if t["same"] and t["boundary_m"] != t["boundary"]:
        s += f"（つなぐと {t['boundary_m']:,}）"
    s += f"・同じ位置の頂点 {t['same']:,}"
    if t["loose"]:
        s += f"・★面の無い頂点 {t['loose']:,}"
    if t["degenerate"]:
        s += f"・★面積ほぼ 0 の面 {t['degenerate']:,}"
    if t["same"] and t["islands_m"] < t["islands"]:
        s += "　★面が同じ位置の頂点で切れている（UV・法線の切れ目など）＝布の計算・重みの塗りで裂ける形。先に結合した写しを使う"
    return s


def topology_line_of(pos, polys, unit_m: float = 1.0) -> str:
    return topo_line(topology(pos, polys, unit_m))


def topo_total(rows: list[dict]) -> str:
    ok = [t for t in rows if "skipped" not in t]
    if not ok:
        return ""
    keys = ("tris", "islands", "islands_m", "boundary", "boundary_m", "same", "loose", "degenerate")
    tot = {k: sum(t[k] for t in ok) for k in keys}
    tot["verts"] = sum(t["verts"] for t in ok)
    return "網目の合計: " + topo_line(tot).split("　★")[0] + (f"（{len(rows) - len(ok)} 個は数えていない）" if len(ok) < len(rows) else "")


# ── 骨と重み ──────────────────────────────────────────────────────────────────

def _ext(pts):
    return [max(p[i] for p in pts) - min(p[i] for p in pts) for i in range(3)]


def _dominant(ext) -> int | None:
    """いちばん長い軸。2 番目より 1.3 倍以上長いときだけ（人型なら高さ）。"""
    order = sorted(range(3), key=lambda i: -ext[i])
    return order[0] if ext[order[1]] <= 0 or ext[order[0]] / ext[order[1]] >= 1.3 else None


def bone_check(entries, unit_cm: float, label: str) -> tuple[list[str], bool]:
    """entries: [(骨の名前, 重みの中心 (x,y,z), 骨の位置 (x,y,z), 重みの合計)]（同じ座標・同じ単位）。
    行と、食い違いを見つけたか。同じ骨が複数のメッシュに出るときは重みで平均する。"""
    by: dict[str, list] = {}
    for name, c, b, w in entries:
        if not w > 0:
            continue
        e = by.setdefault(name, [0.0, 0.0, 0.0, 0.0, b])
        e[0] += c[0] * w
        e[1] += c[1] * w
        e[2] += c[2] * w
        e[3] += w
    bones = [(n, (e[0] / e[3], e[1] / e[3], e[2] / e[3]), e[4]) for n, e in by.items()]
    if not bones:
        return [], False
    ax = "XYZ"
    dist = sorted(((math.dist(c, b), n) for n, c, b in bones), reverse=True)
    med = sorted(d for d, _ in dist)[len(dist) // 2]
    if len(bones) < 3:
        return [f"  骨と重み（{label}・骨 {len(bones)} 本）: 重みの中心と骨の位置の距離 "
                + "・".join(f"{n} {d * unit_cm:.1f} cm" for d, n in dist)], False
    eb, ec = _ext([b for _, _, b in bones]), _ext([c for _, c, _ in bones])
    hb, hc = max(eb), max(ec)
    out = [f"  骨と重み（{label}・重みの付いた骨 {len(bones)} 本）: 重みの中心と骨の位置の距離 中央値 {med * unit_cm:.1f} cm"
           + (f"（骨の広がりの {med / hb:.0%}）" if hb > 0 else ""),
           "    広がり X×Y×Z: 骨の位置 " + "×".join(f"{v * unit_cm:.1f}" for v in eb)
           + " cm ／ 重みの中心 " + "×".join(f"{v * unit_cm:.1f}" for v in ec) + " cm"]
    bad = False
    ratio = hc / hb if hb > 0 else math.inf
    da, dc = _dominant(eb), _dominant(ec)
    if not 1 / 3 <= ratio <= 3:
        out.append(f"    ★大きさが約 {ratio:.3g} 倍ずれている＝単位の食い違いの形（骨と体の片方だけ cm／m・0.01 倍など）")
        bad = True
    elif da is not None and dc is not None and da != dc:
        out.append(f"    ★いちばん長い軸が違う（骨は {ax[da]}・重みの中心は {ax[dc]}）＝向きの食い違いの形（骨は Y が上・体は Z が上など）")
        bad = True
    elif hb > 0 and med / hb > 0.2:
        out.append("    ★重みの中心が骨から離れている（骨の広がりの 2 割を超える）＝骨と体の位置がずれている形")
        bad = True
    else:
        out.append("    骨と重みの位置は揃っている")
    far = [(d, n) for d, n in dist if hb > 0 and d / hb > 0.25][:3]
    if far and not bad:
        out.append("    離れている骨: " + "・".join(f"{n} {d * unit_cm:.1f} cm" for d, n in far))
    return out, bad


# ── VRChat の評価の見積り ────────────────────────────────────────────────────

_RANKS = ("Excellent", "Good", "Medium", "Poor")
# creators.vrchat.com「Performance Ranks」（2026-10-15 に確かめた値）。各段の上限（以下なら入る）。
_LIMITS = {
    "PC": {"tris": (32000, 70000, 70000, 70000), "skinned": (1, 2, 8, 16), "basic": (4, 8, 16, 24),
           "slots": (4, 8, 16, 32), "bones": (75, 150, 256, 400)},
    "Android・Quest": {"tris": (7500, 10000, 15000, 20000), "skinned": (1, 1, 2, 2), "basic": (1, 1, 2, 2),
                       "slots": (1, 1, 2, 4), "bones": (75, 90, 150, 150)},
}
_STAT_JA = {"tris": "三角形", "skinned": "スキンのメッシュ", "basic": "普通のメッシュ", "slots": "マテリアルの枠", "bones": "骨"}


def _rank(v: int, lims) -> int:
    for i, lim in enumerate(lims):
        if v <= lim:
            return i
    return len(lims)                                     # Very Poor


def vrchat_estimate(stats: dict) -> list[str]:
    """stats: tris・skinned・basic・slots・bones。いちばん悪い項目で決まる（VRChat の決め方と同じ）。"""
    out = ["  VRChat の評価の見積り（このファイルの分だけ・揺れもの・テクスチャのメモリ・ライト・パーティクルは含まない）:",
           "    " + "・".join(f"{_STAT_JA[k]} {stats[k]:,}" for k in ("tris", "skinned", "basic", "slots", "bones"))]
    for plat, lims in _LIMITS.items():
        r = {k: _rank(stats[k], lims[k]) for k in lims}
        worst = max(r.values())
        name = _RANKS[worst] if worst < len(_RANKS) else "Very Poor"
        why = [f"{_STAT_JA[k]} {stats[k]:,}" + (f" > {lims[k][-1]:,}" if worst == len(_RANKS) else "")
               for k in lims if r[k] == worst and worst > 0]
        out.append(f"    {plat}: {name}" + (f"（決めているのは {'・'.join(why)}）" if why else ""))
    return out
