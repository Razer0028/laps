#!/usr/bin/env bash
# =============================================================================
# build_pyruntime.sh — Windows 版のアプリに同梱する Python（app/pyruntime/）を組み立てる
#
# アプリ（Tauri）は本体を子プロセスとして起こす: <インストール先>\pyruntime\python.exe -m laps.serve
# 利用者の PC に Python が無くても動くように、次を 1 つの木にまとめる:
#   python.exe ほか        python.org の embeddable package（SHA-256 と MD5 で照合）
#   python312._pth         site-packages と app を通すように書き換えたもの
#   site-packages/         版を固定した wheel を展開したもの
#   app/laps/              src/laps の写し（tests は入れない）
#   app/PROMPTS/           src/PROMPTS の写し
#   INTEGRITY.json         全ファイルの大きさと SHA-256（アプリの「整合性の確認」が使う）
#   WHEELS.txt             入れた wheel の名前・版・SHA-256・ライセンス
#
# 使い方（Linux・macOS・WSL）:
#   bash scripts/build_pyruntime.sh
#   要るもの: python3（3.12 推奨・pip つき）・curl・unzip・rsync
#   LAPS_PYRUNTIME_OUT=<dir>  出力先（既定 app/pyruntime）
#   LAPS_BUILD_CACHE=<dir>    取得物の置き場（既定 .build-cache/pyruntime）
#
# ★symlink を作らない（NSIS のインストーラは symlink を辿らず、中身が空になる）。
# ★tauri.conf.json の bundle.resources は "../pyruntime": "pyruntime"（glob ではなく map の形）。
#   glob にすると階層が平たく落ち、site-packages と app が消える（ビルドは成功してしまう）。
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/src"
OUT="${LAPS_PYRUNTIME_OUT:-${ROOT}/app/pyruntime}"
CACHE="${LAPS_BUILD_CACHE:-${ROOT}/.build-cache/pyruntime}"
PY="${PYTHON:-python3}"

# --- 版の固定 ----------------------------------------------------------------
PY_VER="3.12.10"
PY_TAG="python312"
PY_ZIP="python-${PY_VER}-embed-amd64.zip"
PY_URL="https://www.python.org/ftp/python/${PY_VER}/${PY_ZIP}"
PY_MD5="fe8ef205f2e9c3ba44d0cf9954e1abd3"        # python.org の公表値
PY_SHA256="4acbed6dd1c744b0376e3b1cf57ce906f9dc9e95e68824584c8099a63025a3c3"

# 本体の依存（すべて純 Python）
WHEELS=(
  "httpx==0.28.1" "click==8.4.2" "rich==15.0.0" "httpcore==1.0.9" "h11==0.16.0" "anyio==4.14.2"
  "idna==3.18" "certifi==2026.7.22" "typing_extensions==4.16.0" "markdown-it-py==4.2.0" "mdurl==0.1.2"
  "pygments==2.20.0" "colorama==0.4.6"
)
# 純 Python 版を取る物（websockets は C の高速化つきの wheel もあるが、純 Python 版で足りる）
PURE_WHEELS=("websockets==17.1")
# Doc ツール（Word・PowerPoint・Excel・PDF を作る・読む）の部品。win_amd64 のネイティブ拡張を含む
DOC_WHEELS=(
  "python-docx==1.2.0" "python-pptx==1.0.2" "openpyxl==3.1.5" "reportlab==5.0.1" "matplotlib==3.11.2" "pypdf==6.19.0"
  "lxml==6.1.3" "pillow==12.3.0" "xlsxwriter==3.2.9" "et_xmlfile==2.0.0" "charset_normalizer==3.5.2"
  "numpy==2.5.3" "contourpy==1.4.0" "cycler==0.12.1" "fonttools==4.66.1" "kiwisolver==1.5.1"
  "packaging==26.3" "pyparsing==3.3.3" "python-dateutil==2.9.0.post0" "six==1.17.0" "typing_extensions==4.16.0"
)

msg() { printf '\n=== %s\n' "$*"; }
die() { printf '\n!!! %s\n' "$*" >&2; exit 1; }
mkdir -p "${CACHE}"

# --- 1. embeddable package ---------------------------------------------------
msg "1. embeddable package (${PY_VER})"
ZIP_PATH="${CACHE}/${PY_ZIP}"
if ! [ -f "${ZIP_PATH}" ] || [ "$(sha256sum "${ZIP_PATH}" | cut -d' ' -f1)" != "${PY_SHA256}" ]; then
  curl -fsSL --max-time 300 -o "${ZIP_PATH}.part" "${PY_URL}" || die "取得に失敗: ${PY_URL}"
  mv "${ZIP_PATH}.part" "${ZIP_PATH}"
fi
[ "$(sha256sum "${ZIP_PATH}" | cut -d' ' -f1)" = "${PY_SHA256}" ] || die "SHA-256 が一致しない"
[ "$(md5sum "${ZIP_PATH}" | cut -d' ' -f1)" = "${PY_MD5}" ] || die "MD5 が python.org の公表値と違う"
echo "  照合 OK"

# --- 2. wheel ------------------------------------------------------------------
msg "2. wheel の取得"
WHEEL_DIR="${CACHE}/wheels"; DOC_DIR="${CACHE}/doc-wheels"
rm -rf "${WHEEL_DIR}" "${DOC_DIR}"; mkdir -p "${WHEEL_DIR}" "${DOC_DIR}"
"${PY}" -m pip download -q --only-binary=:all: --platform win_amd64 --python-version 3.12 -d "${WHEEL_DIR}" "${WHEELS[@]}" \
  || die "wheel の取得に失敗"
"${PY}" -m pip download -q --only-binary=:all: --platform any --python-version 3.12 --no-deps -d "${WHEEL_DIR}" "${PURE_WHEELS[@]}" \
  || die "純 Python 版の wheel の取得に失敗"
bad="$(ls -1 "${WHEEL_DIR}"/*.whl | grep -v -- '-none-any\.whl$' || true)"
[ -z "${bad}" ] || die "純 Python でない wheel が混ざっている:\n${bad}"
"${PY}" -m pip download -q --only-binary=:all: --platform win_amd64 --python-version 3.12 -d "${DOC_DIR}" "${DOC_WHEELS[@]}" \
  || die "Doc ツールの部品の取得に失敗"
got="$(ls -1 "${DOC_DIR}"/*.whl | wc -l)"
[ "${got}" -eq "${#DOC_WHEELS[@]}" ] || { ls -1 "${DOC_DIR}"; die "Doc ツールの部品: 取れた ${got} 個 / 固定 ${#DOC_WHEELS[@]} 個（増えた依存を DOC_WHEELS に足す）"; }
echo "  本体の依存 $(ls -1 "${WHEEL_DIR}"/*.whl | wc -l) 個・Doc ツールの部品 ${got} 個"

# --- 3. 組み立て ---------------------------------------------------------------
msg "3. ${OUT} を組み立てる"
rm -rf "${OUT}"
mkdir -p "${OUT}/site-packages" "${OUT}/app"
unzip -q "${ZIP_PATH}" -d "${OUT}"
for w in "${WHEEL_DIR}"/*.whl; do
  if unzip -l "$w" | grep -q '\.data/'; then die "$(basename "$w") は .data/ を持つ（展開先の規則を決めてから入れる）"; fi
  unzip -q -o "$w" -d "${OUT}/site-packages"
done
for w in "${DOC_DIR}"/*.whl; do
  unzip -q -o "$w" -x '*.data/*' -d "${OUT}/site-packages"
done
rsync -a --copy-links --exclude='__pycache__/' --exclude='*.pyc' --exclude='*.pyo' --exclude='tests/' \
  "${SRC}/laps/" "${OUT}/app/laps/"
rsync -a --copy-links "${SRC}/PROMPTS/" "${OUT}/app/PROMPTS/"
find "${OUT}" -name '__pycache__' -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "${OUT}" -name '*.pyc' -delete 2>/dev/null || true
cat > "${OUT}/${PY_TAG}._pth" <<'PTH'
python312.zip
.
app
site-packages
import site
PTH

# 入れた wheel の一覧（名前・版・SHA-256・ライセンス）
"${PY}" - "${OUT}" "${WHEEL_DIR}" "${DOC_DIR}" <<'PYEOF' || die "WHEELS.txt を書けない"
import hashlib, pathlib, sys, zipfile, email.parser
out = pathlib.Path(sys.argv[1]); rows = []
for d in sys.argv[2:]:
    for w in sorted(pathlib.Path(d).glob("*.whl")):
        z = zipfile.ZipFile(w)
        meta = next(n for n in z.namelist() if n.endswith(".dist-info/METADATA"))
        m = email.parser.Parser().parsestr(z.read(meta).decode("utf-8", "replace"))
        lic = m.get("License-Expression") or ""
        if not lic:
            cls = [c.split("::")[-1].strip() for c in m.get_all("Classifier") or [] if c.startswith("License ::")]
            raw = (m.get("License") or "").strip()
            lic = "; ".join(cls) if cls else (raw.splitlines()[0][:60] if raw else "?")
        rows.append(f"{m['Name']}\t{m['Version']}\t{lic}\t{hashlib.sha256(w.read_bytes()).hexdigest()}\t{w.name}")
(out / "WHEELS.txt").write_text("name\tversion\tlicense\tsha256\twheel\n" + "\n".join(rows) + "\n", encoding="utf-8")
print(f"  WHEELS.txt: {len(rows)} 個")
PYEOF

# --- 4. 照合値の一覧 -----------------------------------------------------------
msg "4. INTEGRITY.json"
"${PY}" - "${OUT}" <<'PYEOF' || die "INTEGRITY.json を書けない"
import hashlib, json, os, sys, time
root = sys.argv[1]; files = {}
for dp, dns, fns in os.walk(root):
    dns.sort()
    for fn in sorted(fns):
        full = os.path.join(dp, fn); rel = os.path.relpath(full, root).replace(os.sep, "/")
        if rel == "INTEGRITY.json":
            continue
        h = hashlib.sha256()
        with open(full, "rb") as fh:
            for b in iter(lambda: fh.read(1 << 20), b""):
                h.update(b)
        files[rel] = [os.path.getsize(full), h.hexdigest()]
with open(os.path.join(root, "INTEGRITY.json"), "w", encoding="utf-8") as fh:
    json.dump({"made": time.strftime("%Y-%m-%dT%H:%M:%S"), "files": files}, fh, ensure_ascii=False, separators=(",", ":"))
print(f"  {len(files)} 個のファイル")
PYEOF

# --- 5. 検証 -------------------------------------------------------------------
msg "5. 検証"
fail=0
ok() { echo "  OK   $1"; }
ng() { echo "  NG   $1"; fail=1; }
n="$(find "${OUT}" -type l | wc -l)"; if [ "$n" -eq 0 ]; then ok "symlink なし"; else ng "symlink がある（${n}）"; fi
n="$(find "${OUT}" \( -name '__pycache__' -o -name '*.pyc' \) | wc -l)"; if [ "$n" -eq 0 ]; then ok ".pyc なし"; else ng ".pyc がある（${n}）"; fi
n="$(find "${OUT}/app" -name tests | wc -l)"; if [ "$n" -eq 0 ]; then ok "本体に tests を含まない"; else ng "本体に tests がある"; fi
miss=""; for f in python.exe ${PY_TAG}.dll ${PY_TAG}.zip _socket.pyd _ssl.pyd libssl-3.dll libcrypto-3.dll vcruntime140.dll; do [ -f "${OUT}/$f" ] || miss="${miss} $f"; done
if [ -z "${miss}" ]; then ok "実行に要るファイルが揃っている"; else ng "欠けている:${miss}"; fi
miss=""; for f in guardrails.md honesty.md ethics/direct.md ethics/strict-extra.md; do [ -s "${OUT}/app/PROMPTS/$f" ] || miss="${miss} $f"; done
if [ -z "${miss}" ]; then ok "PROMPTS が揃っている"; else ng "PROMPTS が欠けている:${miss}"; fi
n="$(find "${OUT}" | LC_ALL=C grep -c '[^ -~]' || true)"; if [ "$n" -eq 0 ]; then ok "パスがすべて ASCII"; else ng "非 ASCII のパスがある（${n}）"; fi
# 本体の全モジュール（端末版を除く）が、同梱の純 Python の依存だけで import できるか（この機械の Python で）
if "${PY}" - "${OUT}" <<'PYEOF'
import importlib, pathlib, sys
sys.dont_write_bytecode = True
out = pathlib.Path(sys.argv[1])
sys.path = [p for p in sys.path if "site-packages" not in p and "dist-packages" not in p]
sys.path[:0] = [str(out / "app"), str(out / "site-packages")]
mods = []
for p in sorted((out / "app" / "laps").rglob("*.py")):
    parts = list(p.relative_to(out / "app").with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    m = ".".join(parts)
    if m and not (m == "laps.term" or m.startswith("laps.term.")):
        mods.append(m)
bad = []
for m in mods:
    try:
        importlib.import_module(m)
    except BaseException as e:   # noqa: BLE001
        bad.append(f"{m}: {type(e).__name__}: {e}")
print(f"  本体モジュール {len(mods)} 個を import: 失敗 {len(bad)} 個")
for b in bad[:20]:
    print("    -", b)
sys.exit(1 if bad else 0)
PYEOF
then ok "本体モジュールが同梱の依存で import できる"; else ng "import できないモジュールがある"; fi
[ "${fail}" -eq 0 ] || die "検証に失敗"
du -sh "${OUT}" | sed 's/^/  大きさ /'
echo "  できた: ${OUT}"
