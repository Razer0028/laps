"""Unity のオブジェクトを 1 つずつ読む — Read の find（名前で探す）と path_id（1 つを詳しく）。2026-10-10。

オーナー「ゲーム解析系をさらに強化してください。ログを参考に」。走行記録（10-09〜10-10・オーナー機・Unity の Mono の
ゲーム）でエージェントが時間を使った所:
  ・MonoBehaviour がどの台本（クラス）の物かを突き止めるのに UnityPy の台本を 10 本以上書いた（m_Name と m_ClassName の
    取り違え・PPtr を辿っていない・台本の定義が同じバンドルの別のファイル（globalgamemanagers.assets）にある）。
  ・GameObject の名前・部品・親子（Transform の m_Father / m_Children）を辿る台本を 3 走行・約 2 時間で 60 本近く書いた。
    名前が「Unknown」と出て、読む前に属性を見ていた・m_Parent と m_Father の取り違え、で何度も書き直した。
  ・path_id を 1 つ決めて中身（16 進・参照先）を見る台本を毎回書いた。
⇒ 型の木が無くても形の決まっている組み込みの型（GameObject・Transform・MonoBehaviour の頭・MonoScript・名前を持つ資産）を
  道具が読み、「名前で探す」「1 つを詳しく見る」を Read の引数で返す。

読み方の出典: AssetStudio（MIT）の GameObject / Transform / MonoBehaviour / MonoScript。
読むだけ。書き出しはしない（取り出し方は UnityPy の 1 行を添える）。
"""
from __future__ import annotations

import os
import re
import struct
import time

from . import unityfile as _uf

FIND_BUDGET = 45.0                   # 名前で探すときの時間の上限（秒）。超えたら見た所までを「下限」として返す
MAX_ROWS = 30
CUSTOM_SCAN = 64 * 1024 * 1024       # MonoBehaviour の中身を調べる上限
HEX_DEFAULT = 256
HEX_MAX = 4096

# 名前（m_Name）が最初の項目にある型（NamedObject の子）。
_NAMED = {21, 28, 43, 48, 49, 74, 83, 84, 89, 90, 91, 93, 117, 124, 128, 134, 142, 152, 156, 213, 221, 319, 329,
          687078895}
_TRANSFORMS = (4, 224)
# 頭に m_GameObject（PPtr）を持つ部品（Component の子）。
_COMPONENTS = {4, 8, 20, 23, 33, 54, 56, 58, 61, 64, 65, 68, 81, 82, 92, 95, 108, 111, 114, 120, 121, 137, 171, 198,
               199, 212, 222, 223, 224, 225, 328}

HOWTO = ("→ 名前で探す: Read に find=<文字>（GameObject・台本のクラス・資産の名前・TextAsset の中身。a|b で複数）／1 つを詳しく: Read に "
         "path_id=<番号>（部品・親子・台本・項目の値・参照・中身の 16 進）。UnityPy の台本を書く前にこちらで。")


def _uver(s: str) -> tuple[int, int]:
    m = re.match(r"(\d+)\.(\d+)", s or "")
    return (int(m.group(1)), int(m.group(2))) if m else (2019, 4)


def _q(s: str | None, n: int = 80) -> str:
    s = (s or "").replace("\r", " ").replace("\n", " ")
    return '"' + (s if len(s) <= n else s[:n] + "…") + '"'


def _tname(cls: int) -> str:
    return _uf.CLASS_NAMES.get(cls, f"classID {cls}")


class Unit:
    """1 つのアセット（SerializedFile）。src.read(off, n) で読む（単体のファイルでもバンドルの中でも同じ）。"""

    def __init__(self, label: str, src, base: int, info: dict, file_path: str | None = None) -> None:
        self.label, self.src, self.base, self.info = label, src, base, info
        self.file_path = file_path
        self.ver = info["version"]
        self.e = info["endian"]
        self.uv = _uver(info.get("unity") or "")
        self.objs = {pid: (base + st, sz, cls, ti) for pid, st, sz, cls, ti in info["objects"]}
        self._names: dict[int, str | None] = {}

    # ── 低い層 ──
    def _reader(self, pid: int):
        o = self.objs.get(pid)
        if o is None:
            raise _uf.Short(f"path_id {pid} はありません")
        return _uf.Reader(self.src, o[0], self.e), o

    def pptr(self, r) -> tuple[int, int]:
        fid = r.i32()
        return fid, (r.i64() if self.ver >= 14 else r.i32())

    def string(self, r, start: int, limit: int = 4096) -> str:
        n = r.i32()
        if n < 0 or n > limit:
            raise _uf.Short(f"文字列の長さ {n} が合いません")
        s = r.bytes(n).decode("utf-8", "replace")
        r.align(start)
        return s

    # ── 型ごとの頭 ──
    def gameobject(self, pid: int) -> dict:
        r, (start, _sz, _c, _t) = self._reader(pid)
        n = r.i32()
        if not 0 <= n <= 4096:
            raise _uf.Short(f"部品の数 {n} が合いません")
        comps = []
        for _ in range(n):
            if self.uv < (5, 5):
                r.i32()                                 # 5.5 より前は classID が前に付く
            comps.append(self.pptr(r))
        layer = r.i32()
        return {"components": comps, "layer": layer, "name": self.string(r, start)}

    def transform(self, pid: int) -> dict:
        r, (_start, _sz, _c, _t) = self._reader(pid)
        go = self.pptr(r)
        r.bytes(40)                                     # 回転 4・位置 3・拡大 3（float）
        n = r.i32()
        if not 0 <= n <= 100_000:
            raise _uf.Short(f"子の数 {n} が合いません")
        children = [self.pptr(r) for _ in range(n)]
        return {"go": go, "children": children, "father": self.pptr(r)}

    def behaviour(self, pid: int) -> dict:
        r, (start, sz, _c, _t) = self._reader(pid)
        go = self.pptr(r)
        enabled = r.u8()
        r.align(start)
        script = self.pptr(r)
        name = self.string(r, start)
        return {"go": go, "enabled": enabled, "script": script, "name": name, "data": r.pos, "end": start + sz}

    def monoscript(self, pid: int) -> dict:
        r, (start, _sz, _c, _t) = self._reader(pid)
        name = self.string(r, start)
        if self.uv >= (3, 4):
            r.i32()                                     # m_ExecutionOrder
        r.bytes(4 if self.uv[0] < 5 else 16)            # m_PropertiesHash
        if self.uv[0] < 3:
            self.string(r, start)
        cls = self.string(r, start)
        ns = self.string(r, start) if self.uv[0] >= 3 else ""
        asm = self.string(r, start)
        return {"name": name, "cls": cls, "ns": ns, "asm": asm}

    def owner(self, pid: int) -> tuple[int, int] | None:
        """部品の m_GameObject。"""
        r, _o = self._reader(pid)
        return self.pptr(r)

    def name(self, pid: int) -> str | None:
        if pid in self._names:
            return self._names[pid]
        nm = None
        o = self.objs.get(pid)
        try:
            if o is None:
                nm = None
            elif o[2] == 1:
                nm = self.gameobject(pid)["name"]
            elif o[2] == 114:
                nm = self.behaviour(pid)["name"]
            elif o[2] == 115 or o[2] in _NAMED:
                r, (start, _s, _c, _t) = self._reader(pid)
                nm = self.string(r, start)
        except (_uf.Short, struct.error, UnicodeDecodeError):
            nm = None
        self._names[pid] = nm
        return nm


class Workspace:
    """開いた Unity のファイル（バンドルなら中の全アセット）と、参照先の解決（同じバンドル → 同じフォルダ）。"""

    def __init__(self, path: str) -> None:
        self.path = path
        self.dir = os.path.dirname(os.path.abspath(path))
        self.labels: list[str] = []
        self.units: dict[str, Unit | None] = {}
        self.errors: dict[str, str] = {}
        self._nodes: dict[str, tuple[str, int]] = {}
        self._src = None
        self._bundle = False
        self._siblings: dict[str, Unit | None] = {}
        self._sib_srcs: list = []
        self._listing: dict[str, str] | None = None
        self._scripts: dict[tuple[str, int], dict | None] = {}
        self._mbtype: dict[tuple[str, int], str | None] = {}
        self._asms = None                     # ゲームの Managed の DLL（MonoBehaviour の項目を読むとき・oc/monodecode.py）

    # ── 開く ──
    @classmethod
    def from_bundle(cls, path: str, lay: dict) -> "Workspace":
        from . import fileformats as _ff
        ws = cls(path)
        ws._src = _uf.BundleSource(path, lay["blocks"], _ff.decode_block)
        ws._bundle = True
        for name, off, _sz in lay["nodes"]:
            if name.lower().endswith((".ress", ".resource")):
                continue
            ws.labels.append(name)
            ws._nodes[name.lower()] = (name, off)
        return ws

    @classmethod
    def from_file(cls, path: str) -> "Workspace":
        ws = cls(path)
        ws._src = _uf.FileSource(path)
        label = os.path.basename(path)
        ws.labels.append(label)
        ws._nodes[label.lower()] = (label, 0)
        return ws

    @classmethod
    def open(cls, path: str) -> "Workspace":
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if head.startswith(b"UnityFS\x00"):
            from . import fileformats as _ff
            lay = _ff.bundle_layout(path, size)
            if lay["error"]:
                raise ValueError(lay["error"])
            return cls.from_bundle(path, lay)
        if _uf.looks_serialized(head, size):
            return cls.from_file(path)
        raise ValueError("Unity のバンドル（UnityFS）でもアセット（.assets・level0 など）でもありません")

    def close(self) -> None:
        for s in [self._src] + self._sib_srcs:
            try:
                if s is not None:
                    s.close()
            except Exception:
                pass

    def decoded_bytes(self) -> int:
        return getattr(self._src, "decoded_bytes", 0) if self._bundle else 0

    def unit(self, label: str) -> Unit | None:
        key = label.lower()
        if key in self.units:
            return self.units[key]
        if key not in self._nodes:
            return None
        name, off = self._nodes[key]
        u = None
        try:
            info = _uf.read_serialized(self._src, off, None, deadline=time.time() + FIND_BUDGET)
            u = Unit(name, self._src, off, info, file_path=None if self._bundle else self.path)
        except (_uf.Short, struct.error, IndexError, ValueError) as e:
            self.errors[key] = f"{type(e).__name__}: {e}"
        self.units[key] = u
        return u

    def all_units(self) -> list[Unit]:
        return [u for u in (self.unit(lb) for lb in self.labels) if u is not None]

    # ── 参照先（PPtr の m_FileID）の解決 ──
    def ref(self, unit: Unit, fid: int) -> Unit | None:
        if fid == 0:
            return unit
        ext = unit.info.get("externals") or []
        if not 0 < fid <= len(ext):
            return None
        base = re.split(r"[\\/]", ext[fid - 1])[-1].lower()
        if base in self._nodes:
            return self.unit(base)
        return self._sibling(base)

    def ext_name(self, unit: Unit, fid: int) -> str:
        ext = unit.info.get("externals") or []
        return ext[fid - 1] if 0 < fid <= len(ext) else f"ファイル {fid}"

    def _sibling(self, base: str) -> Unit | None:
        """同じフォルダ（と Resources/）にある .assets を開く（単体のアセットを読んだとき・Windows は名前の大小を区別しない）。"""
        if base in self._siblings:
            return self._siblings[base]
        u = None
        if self._listing is None:
            self._listing = {}
            for d in (self.dir, os.path.join(self.dir, "Resources")):
                try:
                    for n in os.listdir(d):
                        self._listing.setdefault(n.lower(), os.path.join(d, n))
                except OSError:
                    pass
        cand = self._listing.get(base)
        if cand and os.path.isfile(cand) and os.path.abspath(cand) != os.path.abspath(self.path):
            try:
                with open(cand, "rb") as fh:
                    head = fh.read(64)
                if _uf.looks_serialized(head, os.path.getsize(cand)):
                    src = _uf.FileSource(cand)
                    self._sib_srcs.append(src)
                    info = _uf.read_serialized(src, 0, None, deadline=time.time() + FIND_BUDGET)
                    u = Unit(os.path.basename(cand), src, 0, info, file_path=cand)
            except (OSError, _uf.Short, struct.error, IndexError, ValueError):
                u = None
        self._siblings[base] = u
        return u

    def script(self, unit: Unit, fid: int, pid: int) -> dict | None:
        u = self.ref(unit, fid)
        if u is None or pid not in u.objs:
            return None
        key = (u.label.lower(), pid)
        if key not in self._scripts:
            try:
                self._scripts[key] = u.monoscript(pid)
            except (_uf.Short, struct.error, UnicodeDecodeError):
                self._scripts[key] = None
        return self._scripts[key]

    def script_name(self, unit: Unit, fid: int, pid: int) -> str | None:
        ms = self.script(unit, fid, pid)
        if not ms:
            return None
        return (ms["ns"] + "." if ms["ns"] else "") + (ms["cls"] or ms["name"])

    def mb_class(self, unit: Unit, pid: int) -> str | None:
        """MonoBehaviour の台本のクラス名（型の並びから。無ければ m_Script を辿る）。"""
        o = unit.objs.get(pid)
        if o is None:
            return None
        ti = o[3]
        key = (unit.label.lower(), ti)
        types = unit.info["types"]
        if unit.ver >= 17 and 0 <= ti < len(types) and types[ti]["script"] >= 0:
            if key not in self._mbtype:
                si = types[ti]["script"]
                scripts = unit.info["scripts"]
                self._mbtype[key] = self.script_name(unit, *scripts[si]) if 0 <= si < len(scripts) else None
            if self._mbtype[key]:
                return self._mbtype[key]
        try:
            fid, spid = unit.behaviour(pid)["script"]
        except (_uf.Short, struct.error):
            return None
        return self.script_name(unit, fid, spid)

    def label_of(self, unit: Unit, fid: int, pid: int, *, with_name: bool = True) -> str:
        """参照先 1 つを「型 名前 [path_id]」の形に。"""
        if fid == 0 and pid == 0:
            return "（なし）"
        u = self.ref(unit, fid)
        where = "" if fid == 0 else f"{self.ext_name(unit, fid)} の "
        if u is None or pid not in u.objs:
            return f"{where}path_id {pid}（{'読めない外のファイル' if u is None else '見つからない'}）"
        cls = u.objs[pid][2]
        t = _tname(cls)
        if cls == 114:
            sc = self.mb_class(u, pid)
            t = f"MonoBehaviour〈{sc or '台本不明'}〉"
        nm = u.name(pid) if with_name else None
        if cls in _COMPONENTS and cls != 114 and with_name:
            try:
                gf, gp = u.owner(pid)
                gu = self.ref(u, gf)
                nm = gu.name(gp) if gu else None
                return f"{t} [{where}path_id {pid}]（GameObject {_q(nm)}）" if nm is not None else f"{t} [{where}path_id {pid}]"
            except (_uf.Short, struct.error):
                pass
        return f"{t} {_q(nm)} [{where}path_id {pid}]" if nm else f"{t} [{where}path_id {pid}]"

    # ── 目録（Read のふつうの結果）──
    def summarize(self, label: str, *, deadline: float) -> list[str]:
        u = self.unit(label)
        if u is None:
            return [f"  {label}: 目録を読めません — {self.errors.get(label.lower(), '形が合いません')}"]
        return _uf.summarize(u.src, u.base, u.label, deadline=deadline, info=u.info,
                             resolve=lambda fi, lid: self.script_name(u, fi, lid))

    # ── 名前で探す ──
    def find(self, text: str, deadline: float | None = None) -> list[str]:
        qs = [q.strip().lower() for q in re.split(r"[|｜]", text) if q.strip()]
        if not qs:
            return ["find に探す文字を入れてください（例: find=\"Player\"・find=\"Cubism|moc\"）。"]
        t0 = time.time()
        t_end = deadline or (t0 + FIND_BUDGET)

        def hit(*vals) -> bool:
            return any(v and any(q in v.lower() for q in qs) for v in vals)

        qb = [q.encode("utf-8") for q in qs]
        scripts: list[str] = []
        mbs: dict[str, list[tuple[Unit, int, str | None]]] = {}
        gos: list[tuple[Unit, int, str]] = []
        assets: list[tuple[Unit, int, int, str]] = []
        inside: list[str] = []                    # TextAsset の文字・MonoBehaviour の項目の中身に当たった物

        def content(u: Unit, pid: int, skip: int) -> str | None:
            """オブジェクトの中身（頭の skip バイトの後ろ・256KB まで）に当たれば前後の抜き出し。"""
            st, sz = u.objs[pid][0], u.objs[pid][1]
            if sz <= skip:
                return None
            raw = u.src.read(st + skip, min(sz - skip, 256 * 1024))
            low = raw.lower()
            for q in qb:
                i = low.find(q)
                if i >= 0:
                    seg = raw[max(0, i - 40):i + len(q) + 60].decode("utf-8", "replace")
                    return " ".join(seg.replace("\x00", " ").split())
            return None
        seen = total = bad = 0
        bad_why = ""
        stopped = ""
        for u in self.all_units():
            total += len(u.objs)
            for pid, (_st, sz, cls, _ti) in sorted(u.objs.items(), key=lambda kv: kv[1][0]):
                if time.time() > t_end:
                    stopped = "時間の上限"
                    break
                seen += 1
                try:
                    if cls == 115:
                        ms = self.script(u, 0, pid)
                        if ms and hit(ms["cls"], ms["ns"], ms["name"], ms["asm"]):
                            full = (ms["ns"] + "." if ms["ns"] else "") + ms["cls"]
                            scripts.append(f"  {full}（{ms['asm']}） [{u.label} path_id {pid}]")
                    elif cls == 1:
                        nm = u.name(pid)
                        if nm is not None and hit(nm):
                            gos.append((u, pid, nm))
                    elif cls == 114:
                        sc = self.mb_class(u, pid)
                        nm = u.name(pid)
                        if hit(sc, nm):
                            mbs.setdefault(sc or "（台本不明）", []).append((u, pid, nm))
                        elif len(inside) < 200:
                            snip = content(u, pid, 0)
                            if snip:
                                inside.append(f"  MonoBehaviour〈{sc or '台本不明'}〉 [{u.label} path_id {pid}]: …{snip}…")
                    elif cls in _NAMED:
                        nm = u.name(pid)
                        if nm and hit(nm):
                            assets.append((u, pid, cls, nm))
                        elif cls == 49 and len(inside) < 200:
                            snip = content(u, pid, 0)
                            if snip:
                                inside.append(f"  TextAsset {_q(nm)} [{u.label} path_id {pid}]: …{snip}…")
                except (_uf.Short, struct.error, ValueError, IndexError) as e:
                    bad += 1                              # 壊れたブロック・形の合わない物は数えて飛ばす
                    if not bad_why:
                        bad_why = f"{type(e).__name__}: {e}"
                    continue
            if stopped:
                break
        secs = time.time() - t0
        out = [f"Unity のオブジェクトを {_q(text)} で探した（{os.path.basename(self.path)}・中のアセット {len(self.labels)} 個・"
               f"オブジェクト {seen:,} / {total:,} 個を見た・{secs:.1f} 秒）"
               + (f" — ★{stopped}で打ち切り。下の数は下限" if stopped else "")]
        for k, e in self.errors.items():
            out.append(f"  ★{k} の目録は読めませんでした: {e}")
        if bad:
            out.append(f"  ★読めなかったオブジェクト {bad:,} 個（最初の理由: {bad_why}）。下の数は下限")
        if mbs:
            out.append(f"MonoBehaviour（台本ごと・{sum(len(v) for v in mbs.values()):,} 個）:")
            shown = 0
            groups = sorted(mbs.items(), key=lambda kv: -len(kv[1]))
            if len(groups) > 20:
                out.append(f"  （台本 {len(groups)} 種類のうち多い 20 種類）")
            for sc, rows in groups[:20]:
                out.append(f"  {sc} — {len(rows):,} 個:")
                for u, pid, nm in rows[:8]:
                    if shown >= MAX_ROWS:
                        break
                    shown += 1
                    out.append("    " + self._mb_row(u, pid, nm))
                if len(rows) > 8:
                    out.append(f"    …ほか {len(rows) - 8} 個")
        if gos:
            out.append(f"GameObject {len(gos):,} 個:")
            for u, pid, nm in gos[:MAX_ROWS]:
                out.append("  " + self._go_row(u, pid, nm))
            if len(gos) > MAX_ROWS:
                out.append(f"  …ほか {len(gos) - MAX_ROWS} 個")
        if assets:
            out.append(f"名前を持つ資産 {len(assets):,} 個:")
            for u, pid, cls, nm in assets[:MAX_ROWS]:
                out.append(f"  {_tname(cls)} {_q(nm)} [{u.label} path_id {pid}]（{u.objs[pid][1]:,} バイト）")
            if len(assets) > MAX_ROWS:
                out.append(f"  …ほか {len(assets) - MAX_ROWS} 個")
        if scripts:
            out.append(f"台本（MonoScript）{len(scripts)} 個:")
            out += scripts[:MAX_ROWS] + ([f"  …ほか {len(scripts) - MAX_ROWS} 個"] if len(scripts) > MAX_ROWS else [])
        if inside:
            out.append(f"中身に当たった物（TextAsset の文字・MonoBehaviour の項目）{len(inside)}{' 個以上' if len(inside) >= 200 else ' 個'}:")
            out += inside[:MAX_ROWS] + ([f"  …ほか {len(inside) - MAX_ROWS} 個"] if len(inside) > MAX_ROWS else [])
        if not (scripts or mbs or gos or assets or inside):
            out.append("当たりはありません（大文字小文字を区別せず部分一致。台本のクラス名・GameObject・資産の名前・"
                       "TextAsset の中身・MonoBehaviour の項目の中身を見た）。")
        else:
            out.append("→ 1 つを詳しく: Read に path_id=<番号>（部品・親子・台本・項目の値・参照・中身の 16 進）。"
                       + ("バンドルの中で番号が重なるなら path_id=\"<中のファイル名>:<番号>\"。" if len(self.labels) > 1 else ""))
        # ★2026-10-11 の見回り（0.1.393）: find=model3 / model3.json で当たりが台本のクラス名だけだったのに、モデルは
        #   data.unity3d に grep -aob "model3.json" / "motion3.json" を打ち続けた（無い物を探していた）。
        if _CUBISM_JSON_RE.search(text or "") and not (gos or assets or inside):
            out.append("★Live2D の Cubism SDK for Unity は .model3.json・.motion3.json などを取り込みのときに変換するので、"
                       "ビルドしたゲームの中には（TextAsset として置かれていない限り）入っていません。残っているのは "
                       "CubismMoc（.moc3 の中身＝byte[] の項目）・テクスチャ（Texture2D）・動き（AnimationClip と "
                       "CubismFadeMotionData）・モデルの GameObject の部品（CubismModel・CubismParameter など）。"
                       "Read に find=CubismMoc か find=<キャラクターの名前> で探してください。.model3.json が要るなら、"
                       "moc3 とテクスチャを取り出してから自分で書きます。")
        return out

    def _mb_row(self, u: Unit, pid: int, nm: str | None) -> str:
        try:
            h = u.behaviour(pid)
        except (_uf.Short, struct.error):
            return f"[{u.label} path_id {pid}]"
        size = h["end"] - h["data"]
        gf, gp = h["go"]
        if gf == 0 and gp == 0:
            where = "GameObject なし（資産として置かれた ScriptableObject など）"
        else:
            gu = self.ref(u, gf)
            where = f"GameObject {_q(gu.name(gp) if gu else None)}（path_id {gp}）"
        return (f"[{u.label} path_id {pid}]" + (f" 名前 {_q(nm)}" if nm else "") + f"・{where}・中身 {size:,} バイト")

    def _go_row(self, u: Unit, pid: int, nm: str) -> str:
        try:
            go = u.gameobject(pid)
        except (_uf.Short, struct.error):
            return f"{_q(nm)} [{u.label} path_id {pid}]"
        parts = []
        for fid, cp in go["components"][:12]:
            cu = self.ref(u, fid)
            if cu is None or cp not in cu.objs:
                parts.append("?")
                continue
            c = cu.objs[cp][2]
            parts.append(self.mb_class(cu, cp) or "MonoBehaviour" if c == 114 else _tname(c))
        more = len(go["components"]) - 12
        path = self.go_path(u, pid)
        return (f"{_q(nm)} [{u.label} path_id {pid}]" + (f" 階層 {path}" if path and "/" in path else "")
                + " — 部品: " + "・".join(p.rsplit(".", 1)[-1] for p in parts) + (f"・ほか {more}" if more > 0 else ""))

    # ── 1 つを詳しく ──
    def detail(self, pid: int, hex_off=None, hex_len=None, only: str | None = None) -> list[str]:
        hits = [u for u in self.all_units() if pid in u.objs and (not only or u.label.lower() == only.lower())]
        hits.sort(key=lambda u: u.objs[pid][2] == 115)          # 台本（MonoScript）は後ろに
        if not hits:
            return [f"path_id {pid} はこのファイルにありません（中のアセット: {', '.join(self.labels[:8])}"
                    + (f" …ほか {len(self.labels) - 8}" if len(self.labels) > 8 else "") + "）。"
                    "名前から番号を引くには Read に find=<文字>。"]
        out: list[str] = []
        if len(hits) > 1:
            out.append(f"★path_id {pid} は {len(hits)} 個のアセットにあります（アセットごとに番号が振られる）。"
                       + ("全部を出します。" if len(hits) <= 3 else "最初の 3 つを出します。")
                       + f" 1 つに絞るには path_id=\"{hits[0].label}:{pid}\" の形（中のファイル名:番号）。")
        for u in hits[:3]:
            try:
                out += self._detail_one(u, pid, hex_off, hex_len)
            except (_uf.Short, struct.error, UnicodeDecodeError, ValueError, IndexError) as e:
                out.append(f"[{u.label} path_id {pid}] 読む途中で止まりました: {e}")
        return out

    def _where(self, u: Unit, start: int, size: int) -> str:
        if u.file_path:
            return f"ファイルの {start:,}（0x{start:x}）バイト目から {size:,} バイト"
        # ★2026-10-11 の見回り: この数をファイルの byte_offset に渡して「past the end」になった（バンドルは圧縮されている）。
        return (f"バンドルを展開した並びの {start:,} バイト目から {size:,} バイト（中のファイル {u.label}・"
                f"★ファイル上の位置ではない。中身のバイトは同じ Read に path_id と hex=true を足す＝位置はオブジェクトの頭から）")

    def _detail_one(self, u: Unit, pid: int, hex_off, hex_len) -> list[str]:
        start, size, cls, _ti = u.objs[pid]
        out = [f"[{u.label} path_id {pid}] {_tname(cls)}・{self._where(u, start, size)}"]
        hex_from = 0
        spans: dict = {}
        if cls == 1:
            go = u.gameobject(pid)
            out.append(f"  名前 {_q(go['name'], 200)}・レイヤー {go['layer']}・階層 {self.go_path(u, pid) or '?'}")
            out.append(f"  部品 {len(go['components'])} 個:")
            tr = None
            for fid, cp in go["components"][:40]:
                out.append("    " + self.label_of(u, fid, cp, with_name=False))
                cu = self.ref(u, fid)
                if tr is None and cu is not None and cp in cu.objs and cu.objs[cp][2] in _TRANSFORMS:
                    tr = (cu, cp)
            if tr:
                out += self._family(*tr)
        elif cls in _TRANSFORMS:
            t = u.transform(pid)
            out.append("  GameObject: " + self.label_of(u, *t["go"]))
            out += self._family(u, pid)
        elif cls == 114:
            hex_from = self._behaviour_lines(u, pid, out, spans)
        elif cls == 115:
            ms = u.monoscript(pid)
            out.append(f"  クラス {ms['cls']}・名前空間 {ms['ns'] or '（なし）'}・アセンブリ {ms['asm']}・名前 {_q(ms['name'])}")
            out += self._users_of_script(u, pid)
        elif cls in _COMPONENTS:
            out.append("  GameObject: " + self.label_of(u, *u.owner(pid)))
        if cls in _NAMED:
            out.append(f"  名前 {_q(u.name(pid), 200)}")
            if cls == 49:
                out += self._text_asset(u, pid)
            elif cls in (28, 83, 329, 43, 117, 89):
                out += self._stream_ref(u, pid)
        # 16 進（オブジェクトの頭からの位置）。項目を読めた MonoBehaviour は頼まれたときだけ（hex_from == -1）。
        asked = hex_off not in (None, "") or hex_len not in (None, "")
        try:
            off = int(hex_off) if hex_off not in (None, "") else max(0, hex_from)
            ln = int(hex_len) if hex_len not in (None, "") else HEX_DEFAULT
        except (TypeError, ValueError):
            off, ln = max(0, hex_from), HEX_DEFAULT
        if off < 0:
            off = max(0, size + off)
        ln = max(1, min(ln, HEX_MAX, max(0, size - off)))
        if hex_from == -1 and not asked:
            out.append(f"  （中身の 16 進は Read に path_id={pid}・byte_offset・byte_length で）")
        elif off < size:
            from . import bytesview as _bv
            data = u.src.read(start + off, ln)
            out.append(f"  中身の 16 進（オブジェクトの頭から {off:,}〜{off + len(data) - 1:,} バイト目・位置はオブジェクトの頭から）:")
            out.append(_bv.hexdump(data, off))
            if off + ln < size:
                out.append(f"  （続きは Read に path_id={pid}・byte_offset={off + ln}・byte_length（最大 {HEX_MAX}））")
        out.append("  取り出し方（UnityPy・型の木が無くても使える）: "
                   f"obj = next(o for o in UnityPy.load(r\"{self.path}\").objects if o.path_id == {pid}"
                   + (f" and o.assets_file.name == \"{u.label}\"" if self._bundle and len(self.labels) > 1 else "")
                   + "); raw = obj.get_raw_data()  # この 16 進と同じ並び（位置はオブジェクトの頭から）")
        out += self._save_lines(u, pid, spans)
        return out

    def _save_lines(self, u: Unit, pid: int, spans: dict) -> list[str]:
        """byte[] の項目に入った形式（MOC3・PNG など）を、そのままファイルに書き出す 1 行（走らせればよい形）。

        ★2026-10-11 の見回り（0.1.387）: path_id が「byte[5,762,240]＝+44〜+5762284・中身は MOC3」と出したのに、モデルは
          get_raw_data() ではなく obj.read() で台本を書いて失敗し（型の木なし）、圧縮されたバンドルの生の位置から偽の .moc3 を
          4 本切り出した。取り出しを台本書きに任せず、位置まで入った 1 行を渡す。"""
        items = spans.get("items") or []
        if not items:
            return []
        pick = (f"next(o for o in UnityPy.load(r'{self.path}').objects if o.path_id == {pid}"
                + (f" and o.assets_file.name == '{u.label}'" if self._bundle and len(self.labels) > 1 else "") + ")")
        base = re.sub(r'[\\/:*?"<>|\s]+', "_", str(spans.get("name") or "")).strip("_.") or f"pid{pid}"
        out = ["  ★中身をファイルに書き出す（この 1 行をそのまま走らせる・obj.read() は型の木が無いと失敗する・位置は道具が数えた）:"]
        import shutil
        py = "py" if os.name == "nt" and shutil.which("py") else ("python3" if shutil.which("python3") else "python")
        seen: dict[str, int] = {}
        for fmt, a, b in items[:4]:
            ext = _SAVE_EXT.get(fmt, ".bin")
            seen[ext] = seen.get(ext, 0) + 1
            fn = base + (f"_{seen[ext]}" if seen[ext] > 1 else "") + ext
            out.append(f"    {fmt}: {py} -c \"import UnityPy; open(r'{fn}', 'wb').write({pick}.get_raw_data()[{a}:{b}])\"")
        if len(items) > 4:
            out.append(f"    …ほか {len(items) - 4} 個（位置は上の「中に埋め込まれた形式」）")
        return out

    def go_path(self, u: Unit, gpid: int) -> str | None:
        """GameObject の階層の道筋（「親/子/孫」）。Transform の m_Father を上へ辿る（32 段まで）。"""
        try:
            go = u.gameobject(gpid)
        except (_uf.Short, struct.error):
            return None
        tr = None
        for fid, cp in go["components"]:
            cu = self.ref(u, fid)
            if cu is not None and cp in cu.objs and cu.objs[cp][2] in _TRANSFORMS:
                tr = (cu, cp)
                break
        names = [go["name"]]
        seen = set()
        while tr and len(names) < 32:
            tu, tp = tr
            if (tu.label, tp) in seen:
                break
            seen.add((tu.label, tp))
            try:
                ff, fp = tu.transform(tp)["father"]
            except (_uf.Short, struct.error):
                break
            if ff == 0 and fp == 0:
                break
            pu = self.ref(tu, ff)
            if pu is None or fp not in pu.objs:
                names.append("?")
                break
            try:
                gf, gp = pu.transform(fp)["go"]
                gu = self.ref(pu, gf)
                names.append((gu.name(gp) if gu else None) or "?")
            except (_uf.Short, struct.error):
                break
            tr = (pu, fp)
        return "/".join(reversed(names))

    def _family(self, u: Unit, tpid: int) -> list[str]:
        """Transform から親と子（の GameObject の名前）。"""
        out = []
        try:
            t = u.transform(tpid)
        except (_uf.Short, struct.error):
            return ["  （Transform を読めません）"]
        f_fid, f_pid = t["father"]
        out.append("  親: " + (self._go_of_transform(u, f_fid, f_pid) if (f_fid or f_pid) else "（なし＝いちばん上）"))
        kids = t["children"]
        out.append(f"  子 {len(kids)} 個:")
        for cf, cp in kids[:MAX_ROWS]:
            out.append("    " + self._go_of_transform(u, cf, cp))
        if len(kids) > MAX_ROWS:
            out.append(f"    …ほか {len(kids) - MAX_ROWS} 個")
        return out

    def _go_of_transform(self, u: Unit, fid: int, tpid: int) -> str:
        tu = self.ref(u, fid)
        if tu is None or tpid not in tu.objs:
            return f"Transform [path_id {tpid}]（読めない）"
        try:
            gf, gp = tu.transform(tpid)["go"]
            gu = self.ref(tu, gf)
            nm = gu.name(gp) if gu else None
            nkids = len(tu.transform(tpid)["children"])
        except (_uf.Short, struct.error):
            return f"Transform [path_id {tpid}]"
        return f"GameObject {_q(nm)} [path_id {gp}]（Transform {tpid}・子 {nkids}）"

    def _users_of_script(self, u: Unit, spid: int) -> list[str]:
        """この台本を使う MonoBehaviour の数（型の並びから・各アセット）。"""
        rows = []
        for v in self.all_units():
            types, scripts = v.info["types"], v.info["scripts"]
            tis = set()
            for ti, t in enumerate(types):
                si = t.get("script", -1)
                if 0 <= si < len(scripts):
                    fi, lid = scripts[si]
                    if lid == spid and self.ref(v, fi) is u:
                        tis.add(ti)
            n = sum(1 for _p, (_s, _z, c, ti) in v.objs.items() if c == 114 and ti in tis)
            if n:
                first = [p for p, (_s, _z, c, ti) in v.objs.items() if c == 114 and ti in tis][:5]
                rows.append(f"    {v.label}: {n:,} 個（path_id {', '.join(map(str, first))}{' …' if n > 5 else ''}）")
        return (["  この台本の MonoBehaviour:"] + rows) if rows else []

    def _stream_ref(self, u: Unit, pid: int) -> list[str]:
        """本体が外（.resS・.resource）にある資産の、置き場所と位置・大きさ（走行記録では AudioClip が「stub」と出て迷った）。

        AudioClip の m_Resource＝文字列（置き場所）→ 位置 u64 → 大きさ u64。Texture2D などの m_StreamData＝位置（u32 か
        u64）→ 大きさ u32 → 文字列。型の木が無くても、置き場所の文字列の前後で見分ける。"""
        start, size = u.objs[pid][0], u.objs[pid][1]
        data = u.src.read(start, min(size, 1 << 20))
        out = []
        for off, s in _strings(data):
            low = s.lower()
            if not low.endswith((".ress", ".resource")) and ".ress" not in low and ".resource" not in low:
                continue
            end = off + 4 + len(s.encode("utf-8"))
            end += (-end) % 4
            cands = []
            if end + 16 <= len(data):                          # 後ろに u64 位置・u64 大きさ（AudioClip・VideoClip）
                o, z = struct.unpack_from(u.e + "QQ", data, end)
                cands.append((o, z))
            for back, fmt in ((12, "QI"), (8, "II")):           # 前に位置・大きさ（Texture2D など）
                if off - back >= 0:
                    o, z = struct.unpack_from(u.e + fmt, data, off - back)
                    cands.append((o, z))
            for o, z in cands:
                if 0 < z < (1 << 34) and o < (1 << 40):
                    src = s.split("/")[-1]
                    local = None
                    if u.file_path:
                        p = os.path.join(os.path.dirname(u.file_path), src)
                        local = p if os.path.isfile(p) else None
                    if local:
                        tail = f" → Read に file_path={local}・hex=true・byte_offset={o}（FSB5・画像の頭が見える）"
                    else:
                        # ★2026-10-13: data.unity3d を展開した .assets の隣には .resource が無い（展開前の <Game>_Data に
                        #   data.unity3d と並んで置かれる）。旧の案内「バンドルの中」は外れで、PC の走行は展開フォルダの別のファイル
                        #   （画像の .resS）を音声の置き場と取り違えて 40 分空回りした。作業場から同じ名前のファイルを探して名指しする。
                        hits = []
                        try:
                            from . import whereis as _wi
                            hits = [h for h in (_wi._index(os.getcwd()) or {}).get(src.lower(), []) if not h.endswith(os.sep)]
                        except Exception:
                            hits = []
                        if hits:
                            tail = (f"（この .assets の隣には無い。作業場で同じ名前のファイル: {', '.join(hits[:3])}"
                                    f" → その {o:,} バイト目から {z:,} バイト）")
                        else:
                            tail = ("（この .assets の隣には無い。data.unity3d から取り出した .assets なら、.resource は展開前の "
                                    "<Game>_Data に data.unity3d と並んで置かれている。UnityPy は _Data ごと読ませると一緒に読む）")
                    out.append(f"  本体は外: {s} の {o:,}（0x{o:x}）バイト目から {z:,} バイト" + tail)
                    break
            if out:
                break
        return out

    def _text_asset(self, u: Unit, pid: int) -> list[str]:
        r, (start, _sz, _c, _t) = u._reader(pid)
        u.string(r, start)
        n = r.i32()
        if n < 0:
            return []
        body = u.src.read(r.pos, min(n, 600))
        try:
            txt = body.decode("utf-8")
            return [f"  中身 {n:,} バイト（文字）: {_q(txt, 400)}"]
        except UnicodeDecodeError:
            return [f"  中身 {n:,} バイト（文字ではない・頭 {body[:16].hex(' ')}）"]

    def _behaviour_lines(self, u: Unit, pid: int, out: list[str], spans: dict | None = None) -> int:
        """MonoBehaviour の頭と、型の木が無くても分かる中身の手掛かり。戻り値＝16 進を見せ始める位置。"""
        h = u.behaviour(pid)
        start, size = u.objs[pid][0], u.objs[pid][1]
        out.append("  GameObject: " + self.label_of(u, *h["go"]))
        ms = self.script(u, *h["script"])
        if ms:
            out.append(f"  台本: {(ms['ns'] + '.') if ms['ns'] else ''}{ms['cls']}（{ms['asm']}）"
                       f" [{'' if h['script'][0] == 0 else self.ext_name(u, h['script'][0]) + ' の '}path_id {h['script'][1]}]")
        else:
            out.append(f"  台本: 読めません（m_Script = ファイル {h['script'][0]}・path_id {h['script'][1]}）")
        out.append(f"  名前 {_q(h['name'])}・有効 {bool(h['enabled'])}")
        if spans is not None:
            spans.update(name=h["name"], items=[])
        dstart = h["data"] - start
        dsize = h["end"] - h["data"]
        tree = u.info.get("type_tree")
        out.append(f"  台本の項目（頭より後）: {dsize:,} バイト（オブジェクトの頭から {dstart} バイト目〜）"
                   + ("・型の木あり＝UnityPy の read_typetree() で項目名つきで読めます" if tree else
                      "・★型の木なし（UnityPy の read_typetree() は ValueError になる）"))
        if dsize <= 0:
            return dstart
        data = u.src.read(h["data"], min(dsize, CUSTOM_SCAN))
        # ★型の木が無ければ、ゲームの Managed/*.dll のクラスの宣言から項目を読む（oc/monodecode.py）。
        decoded_all = False
        if not tree and ms and len(data) == dsize:
            from . import monodecode as _md
            if self._asms is None:
                md = _md.managed_dir(self.path)
                self._asms = _md.Assemblies(md) if md else False
            try:
                rows = _md.decode_behaviour(self._asms.folder if self._asms else None, ms["asm"], ms["ns"], ms["cls"],
                                            data, u.e, u.ver, lambda f, p: self.label_of(u, f, p),
                                            self._asms or None, base_off=dstart, unity=u.uv)
            except Exception as e:  # noqa: BLE001 — 読めなければ下の推定に任せる
                rows = [f"  項目の値: 読む途中で止まりました（{type(e).__name__}: {e}）"]
            out += rows
            decoded_all = any("一致＝" in r for r in rows[-1:])
        if decoded_all:
            out += _embedded(data, dstart, start if u.file_path else None, spans)
            return -1                                   # 読めたので 16 進は頼まれたときだけ
        if not tree:
            out.append("  並びからの推定（項目名なし）。項目の並びと型はゲームの Managed/*.dll のクラスの宣言順"
                       "（Read で .dll を開き find=<クラス名>）:")
        strs = _strings(data)
        if strs:
            out.append("  文字列らしき項目: " + "・".join(f"+{dstart + o}: {_q(s, 60)}" for o, s in strs[:12])
                       + (f"・ほか {len(strs) - 12}" if len(strs) > 12 else ""))
        refs = self._refs(u, data, dstart, {o for o, _s in strs})
        if refs:
            out.append("  参照らしき項目（PPtr＝m_FileID 4 バイト＋m_PathID 8 バイトの並びが実在の物を指すもの・推定）:")
            out += ["    " + r for r in refs[:12]]
            if len(refs) > 12:
                out.append(f"    …ほか {len(refs) - 12}")
        out += _embedded(data, dstart, start if u.file_path else None, spans)
        return dstart

    def _refs(self, u: Unit, data: bytes, dstart: int, skip: set[int]) -> list[str]:
        out = []
        step = 12 if u.ver >= 14 else 8
        p = 0
        n = len(data)
        while p + step <= n and len(out) < 40:
            fid = struct.unpack_from(u.e + "i", data, p)[0]
            pid = (struct.unpack_from(u.e + "q", data, p + 4)[0] if u.ver >= 14
                   else struct.unpack_from(u.e + "i", data, p + 4)[0])
            ok = False
            if pid != 0 and 0 <= fid <= len(u.info.get("externals") or []) and p not in skip:
                tu = self.ref(u, fid)
                ok = tu is not None and pid in tu.objs
            if ok:
                small = abs(pid) < 1_000_000
                out.append(f"+{dstart + p}: → {self.label_of(u, fid, pid)}"
                           + ("（番号が小さいので数値の偶然の一致もありうる）" if small and fid == 0 else ""))
                p += step
            else:
                p += 4
        return out


def _strings(data: bytes, limit: int = 64) -> list[tuple[int, str]]:
    """4 バイト境界の「長さ＋UTF-8」の並び（Unity の文字列の項目）。"""
    out = []
    p = 0
    n = len(data)
    while p + 5 <= n and len(out) < limit:
        ln = struct.unpack_from("<i", data, p)[0]
        if 1 <= ln <= 1024 and p + 4 + ln <= n:
            b = data[p + 4:p + 4 + ln]
            try:
                s = b.decode("utf-8")
            except UnicodeDecodeError:
                s = None
            if s and s.isprintable() and sum(ch.isalnum() for ch in s) >= max(1, len(s) // 3):
                out.append((p, s))
                p += 4 + ln
                p += (-p) % 4
                continue
        p += 4
    return out


# Cubism の JSON の名前（model3・motion3・physics3・cdi3・exp3・pose3・userdata3）で探されたとき（find の注記）。
_CUBISM_JSON_RE = re.compile(r"(?:model|motion|physics|cdi|exp|pose|userdata)3(?:\.json)?", re.I)

_SAVE_EXT = {"PNG": ".png", "JPEG": ".jpg", "GIF": ".gif", "RIFF": ".wav", "Ogg": ".ogg", "FLAC": ".flac", "MP3(ID3)": ".mp3",
             "FSB5": ".fsb", "UnityFS": ".bundle", "ZIP": ".zip", "7z": ".7z", "MOC3": ".moc3", "SQLite": ".db", "PDF": ".pdf",
             "DDS": ".dds", "KTX": ".ktx", "OpenType": ".otf", "WOFF": ".woff", "gzip": ".gz", "Wwise bank": ".bnk"}


def _embedded(data: bytes, dstart: int, file_base: int | None, spans: dict | None = None) -> list[str]:
    """中に埋め込まれた形式（MOC3・PNG・Ogg…）と、直前の 4 バイトが長さ（byte[] の項目）なら大きさ。"""
    from . import bytesview as _bv
    rows = []
    for m in _bv._SIG_RE.finditer(data):
        name = _bv._SIG_NAME[m.group(0)]
        i = m.start()
        if not _bv._sig_ok(name, data, i):
            continue
        ln = struct.unpack_from("<i", data, i - 4)[0] if i >= 4 else -1
        size = f"・直前の 4 バイトが長さ {ln:,}（byte[] の項目）＝+{dstart + i}〜+{dstart + i + ln}" if 0 < ln <= len(data) - i else ""
        if size and spans is not None:
            spans.setdefault("items", []).append((name, dstart + i, dstart + i + ln))
        absn = f"・ファイルの {file_base + dstart + i:,} バイト目" if file_base is not None else ""
        rows.append(f"    {name} がオブジェクトの頭から +{dstart + i}{size}{absn}")
        if len(rows) >= 10:
            break
    return (["  中に埋め込まれた形式:"] + rows) if rows else []


# ── Read の入口 ──────────────────────────────────────────────────────────

def read_option(path: str, inp: dict) -> list[str]:
    """Read の find / path_id（Unity のファイル）。結果の行。"""
    ws = Workspace.open(path)
    try:
        if inp.get("path_id") not in (None, ""):
            raw = str(inp.get("path_id")).strip()
            only = None
            if ":" in raw:                                   # 「中のファイル名:番号」（バンドルで番号が重なるとき）
                only, raw = raw.rsplit(":", 1)
            try:
                pid = int(raw)
            except ValueError:
                return [f"path_id は整数か「中のファイル名:整数」で（受け取った値: {inp.get('path_id')!r}）。"]
            return ws.detail(pid, inp.get("byte_offset"), inp.get("byte_length"), only=only)
        return ws.find(str(inp.get("find") or ""))
    finally:
        ws.close()


def _is_unity_file(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
        return head.startswith(b"UnityFS\x00") or _uf.looks_serialized(head, os.path.getsize(path))
    except OSError:
        return False


def find_in_folder(folder: str, text: str, path_id=None) -> list[str]:
    """フォルダ（<Game>_Data など）の中の Unity のファイルを全部探す（3 階層・500 個まで・時間は全体で FIND_BUDGET）。"""
    if path_id not in (None, ""):
        return ["path_id はファイルごとの番号です。ファイル（.assets・level0・data.unity3d・バンドル）を指定してください。"
                "番号は find の結果に [ファイル path_id N] の形で出ます。"]
    t_end = time.time() + FIND_BUDGET
    files: list[str] = []
    base_depth = folder.rstrip("\\/").count(os.sep)
    for root, dirs, names in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in ("Managed", "MonoBleedingEdge", "il2cpp_data", "Plugins"))
        if root.count(os.sep) - base_depth >= 3:
            dirs[:] = []
        for n in sorted(names):
            if n.lower().endswith((".ress", ".resource", ".dll", ".json", ".txt", ".config", ".info", ".png")):
                continue
            p = os.path.join(root, n)
            if _is_unity_file(p):
                files.append(p)
            if len(files) >= 500:
                break
    if not files:
        return [f"{folder} に Unity のファイル（バンドル・.assets・level0 など）がありません。"]
    out = [f"フォルダの Unity のファイル {len(files)} 個を {text!r} で探します（当たりの無いファイルは 1 行）。"]
    for k, p in enumerate(files):
        if time.time() > t_end:
            out.append(f"★時間の上限（{FIND_BUDGET:.0f} 秒）で、残り {len(files) - k} 個のファイルは見ていません: "
                       + ", ".join(os.path.basename(x) for x in files[k:k + 10]))
            break
        rel = os.path.relpath(p, folder)
        try:
            ws = Workspace.open(p)
        except (ValueError, OSError) as e:
            out.append(f"  {rel}: 開けません — {e}")
            continue
        try:
            rows = ws.find(text, deadline=t_end)
        finally:
            ws.close()
        if any(r.startswith(("MonoBehaviour", "GameObject", "名前を持つ", "台本")) for r in rows):
            out.append(f"── {rel}")
            out += rows
        else:
            out.append(f"  {rel}: 当たりなし" + ("（★時間の上限で途中まで）" if "打ち切り" in rows[0] else ""))
    return out


def lookup(path: str, inp: dict) -> list[str]:
    """Read（と MCP の inspect_file）の find / path_id の入口。フォルダ・.NET の DLL・Unity のファイルを振り分ける。

    読めない形なら ValueError（理由つき）。"""
    from . import dotnetmeta as _dm
    if os.path.isdir(path):
        return find_in_folder(path, str(inp.get("find") or ""), inp.get("path_id"))
    with open(path, "rb") as fh:
        head = fh.read(4096)
    from . import uefile as _ue
    if _ue.is_ue(path, head):                       # Unreal の .pak・.utoc・.uasset・.locres（2026-10-11）
        if inp.get("find") in (None, ""):
            raise ValueError("path_id は Unity のファイル用です。Unreal の入れ物・パッケージ・翻訳の表は find=<語> で名前を絞れます。")
        rows = _ue.describe(path, head, os.path.getsize(path), find=str(inp.get("find")))
        if not rows:
            raise ValueError("Unreal Engine のファイルとして読めませんでした")
        return rows
    if _dm.is_dotnet(head):
        if inp.get("find") in (None, ""):
            raise ValueError("path_id は Unity のファイル用です。.NET の DLL は find=<クラス名か項目名> で。")
        return _dm.find(path, str(inp.get("find")))
    return read_option(path, inp)
