"""画面写真をモデルに見せる前の支度（2026-11-15 オーナー「LAPS の GUI 操作についても性能を向上させたい。labeye のシステムを組み込めない？」）。

ラボの labeye（labeye（画像を読む道具））の考え方から 3 つを借りた（コードは書き直したもの）:
  ① 読み手の上限に合わせてこちらで縮め、どれだけ縮めたかをモデルに伝える（labeye info）。
     推論機の画像の読み手は 1 枚 1,024 トークンまで＝それを超える写真は読み手が黙って縮めていた。モデルは縮み方を知らずに
     小さな字やボタンを読もうとしていた。
  ② 0〜1000 の目盛りを写真に重ねる（labeye grid）。rx・ry（写真の上の 0〜1000）を目分量でなく目盛りから読める。
     線は写真の内側に引く＝写真の縁が 0 と 1000 のまま（外に余白を足すと、モデルが余白込みで位置を数えて外す）。
  ③ 点を近くの枠へ寄せる（labeye snap）。枠の少し外を指した点を、その枠の中心にする（Browser の look の枠）。

Pillow だけで動く（アプリに同梱の Python にもある）。開けない・描けないときは元の写真をそのまま返す（操作を止めない）。
"""
from __future__ import annotations

import io
import os
from dataclasses import dataclass

#: 読み手が 1 枚で見る画素の上限の既定。1,024 トークン × 28×28 画素（Qwen 系の読み手の 1 トークン）。
#: ★1 トークンが 32×32 の読み手でもこの値なら上限の中に収まる（少し余計に縮めるだけ）。
DEFAULT_PIXELS = 1024 * 28 * 28
GRID_STEP = 100


@dataclass
class Shown:
    w0: int                 # 撮った大きさ
    h0: int
    w: int                  # モデルに見せる大きさ
    h: int
    grid: bool

    @property
    def scale(self) -> float:
        return self.w / self.w0 if self.w0 else 1.0


def reader_pixels() -> int:
    try:
        v = int(os.environ.get("LAPS_VISION_MAX_PIXELS") or 0)
    except ValueError:
        v = 0
    return v if v >= 64 * 64 else DEFAULT_PIXELS


def grid_on() -> bool:
    return (os.environ.get("LAPS_LOOK_GRID") or "1").strip().lower() not in ("0", "off", "false", "no")


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                      # Pillow が古い（10.1 より前）
        return ImageFont.load_default()


def draw_grid(im):
    """0〜1000 の目盛り: 100 ごとに細い線（明るい背景でも暗い背景でも見える 2 色の破線）と、上と左の縁に数字。"""
    from PIL import Image, ImageDraw
    w, h = im.size
    base = im.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    size = max(9, min(11, w // 120))           # 小さく（縁の字を隠しすぎない）
    font = _font(size)
    dash, gap = 6, 6
    for k in range(GRID_STEP, 1000, GRID_STEP):
        x = round(k * w / 1000)
        y = round(k * h / 1000)
        for y0 in range(0, h, dash + gap):            # 縦の破線（黒と黄を交互に＝どの背景でも見える）
            d.line([(x, y0), (x, min(h, y0 + dash))], fill=(255, 220, 0, 85), width=1)
            d.line([(x + 1, y0), (x + 1, min(h, y0 + dash))], fill=(0, 0, 0, 90), width=1)
        for x0 in range(0, w, dash + gap):
            d.line([(x0, y), (min(w, x0 + dash), y)], fill=(255, 220, 0, 85), width=1)
            d.line([(x0, y + 1), (min(w, x0 + dash), y + 1)], fill=(0, 0, 0, 90), width=1)
        for label, (lx, ly) in ((str(k), (x + 2, 1)), (str(k), (1, y + 1))):
            box = d.textbbox((lx, ly), label, font=font)
            d.rectangle([box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1], fill=(0, 0, 0, 120))
            d.text((lx, ly), label, fill=(255, 230, 60, 255), font=font)
    return Image.alpha_composite(base, layer)


def prepare(raw: bytes, *, grid: bool | None = None, pixels: int | None = None) -> tuple[bytes, Shown] | None:
    """撮った写真（PNG など）を、読み手の上限に収めて目盛りを重ねた PNG にする。できなければ None（元のまま使う）。"""
    try:
        from PIL import Image
        im = Image.open(io.BytesIO(raw))
        im.load()
        w0, h0 = im.size
        limit = pixels or reader_pixels()
        im = im.convert("RGB")
        if w0 * h0 > limit:
            f = (limit / float(w0 * h0)) ** 0.5
            im = im.resize((max(1, int(w0 * f)), max(1, int(h0 * f))), Image.LANCZOS)
        use_grid = grid_on() if grid is None else grid
        if use_grid:
            im = draw_grid(im)
        buf = io.BytesIO()
        im.convert("RGB").save(buf, "PNG", optimize=True)
        return buf.getvalue(), Shown(w0, h0, im.size[0], im.size[1], use_grid)
    except Exception:
        return None


def note(s: Shown, *, zoom_hint: bool = True) -> str:
    """写真の添え書き（モデルあて・英語）。"""
    parts = []
    if s.w != s.w0:
        parts.append(f"The picture is shown at {round(s.scale * 100)}% ({s.w}x{s.h} of {s.w0}x{s.h0} pixels)")
    if s.grid:
        parts.append("dashed lines with numbers mark every 100 on the 0-1000 scale across (rx/x) and down (ry/y); read "
                     "positions from them instead of estimating")
    out = "; ".join(parts)
    if out:
        out = out[0].upper() + out[1:] + "."
    if zoom_hint and s.scale < 0.8:
        out += (" Small text and icons may be unreadable at this size: look again with region=[left, top, right, bottom] "
                "(0-1000) to see that part at full size before you read or click it.")
    return out.strip()


def snap(px: float, py: float, boxes: list[dict], reach: float = 16.0) -> tuple[float, float, dict | None]:
    """点（ページの画素）が枠の外で、いちばん近い枠まで reach 画素以内なら、その枠の中心に寄せる。枠の中ならそのまま。
    boxes: {"x","y","w","h"}（同じ座標）。戻り値: (x, y, 寄せた枠 か None)。"""
    best, best_d = None, reach
    for b in boxes:
        x, y, w, h = float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"])
        if x <= px <= x + w and y <= py <= y + h:
            return px, py, None
        dx = max(x - px, 0.0, px - (x + w))
        dy = max(y - py, 0.0, py - (y + h))
        dist = (dx * dx + dy * dy) ** 0.5
        if dist <= best_d:
            best, best_d = b, dist
    if best is None:
        return px, py, None
    return float(best["x"]) + float(best["w"]) / 2, float(best["y"]) + float(best["h"]) / 2, best
