"""`bwtool` — モデルが書くスクリプトの中から Blackwall の道具を呼ぶ（経緯と台本の置き方は laps/oc/bwtool.py）。

使い方:
  bwtool list                              呼べる道具の一覧
  bwtool help <Tool>                       その道具の説明と引数
  bwtool <Tool> key=value [key=value ...]  道具を呼ぶ（値は道具の引数の型で読む: 文字はそのまま・配列は JSON）
  bwtool <Tool> --input '{"key": ...}'     引数を JSON で（標準入力から読むなら --input -）
  --json                                   結果を {"ok": ..., "text": ...} の JSON で出す
終了コード: 0 成功・1 道具が失敗を返した・2 使い方の誤りか、許可の決まりで断った。

★道具は本体と同じ物をそのまま走らせる（oc/tools.specs の handler）。引数の読み替え・型合わせ・足りない引数の案内も同じ
  （oc/tools.normalize_input・adapt_keys・coerce_input・missing_required・args_help）＝道具版と台本版で挙動が割れない。
★許可の決まり（LAPS_POLICY）は道具の中の検査（読む場所・書く場所）に加えて、ここでも見る: readonly のときは
  読むだけの道具（ToolSpec.readonly）しか走らせない。画面での承認が要る物はそもそも呼べる一覧に入れていない。
"""
from __future__ import annotations

import json
import sys
import time

USAGE = ("usage: bwtool list | bwtool help <Tool> | bwtool <Tool> key=value ... [--input JSON|-] [--json] | bwtool wait ...\n"
         "Calls an L.A.P.S tool from a script. Prints the result; exit code 0 ok, 1 the tool failed, 2 usage or refused.")


class _Text(str):
    """key=value で受けた値（まだ文字のまま）。読み替え（normalize_input・adapt_keys）で名前が変わっても印が残るので、
    最後に道具の引数の型を見てから読む（_typed）。"""


def _parse_value(v: str):
    """型の分からない引数（道具の schema に無い名前）だけに使う: JSON として読めれば数・真偽・配列、読めなければ文字。"""
    try:
        return json.loads(v)
    except ValueError:
        return v


def _typed(spec, inp: dict) -> dict:
    """key=value の値を、道具の引数の型に合わせて読む。
    ★2026-10-25 敵対レビュー: 旧は型を見ずに全部を JSON として読み、文字の引数が数や真偽に化けていた
      （`query=3.10` は 3.1・`q=1e3` は 1000.0・`query=true` は True・`query=0` は 0＝偽なので「query が無い」）。
      文字の引数は文字のまま。配列・物は JSON として読む。数・真偽は文字のまま渡し、coerce_input が型に合わせる。"""
    props = ((spec.schema or {}).get("properties") or {}) if spec is not None else {}
    out = {}
    for k, v in inp.items():
        if not isinstance(v, _Text):
            out[k] = v
            continue
        raw = str(v)
        t = (props.get(k) or {}).get("type") if isinstance(props.get(k), dict) else None
        types = set(t) if isinstance(t, list) else ({t} if t else set())
        if types & {"array", "object"}:
            try:
                got = json.loads(raw)
            except ValueError:
                got = raw
                # シェルが引用符を外した `[/a.wav,/b.wav]`・Windows の `["C:\a.wav"]`（JSON では \a が読めない）も並びとして読む
                if "array" in types and raw.startswith("[") and raw.endswith("]"):
                    got = [x.strip().strip("\"'") for x in raw[1:-1].split(",") if x.strip().strip("\"'")]
            out[k] = got if isinstance(got, (list, dict)) or "string" in types else raw
        elif types:
            out[k] = raw
        else:
            out[k] = _parse_value(raw)
    return out


def _out(text: str, ok: bool, as_json: bool) -> None:
    if as_json:
        sys.stdout.write(json.dumps({"ok": ok, "text": text}, ensure_ascii=False) + "\n")
    else:
        sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()


def _specs() -> dict:
    from .oc import tools as oc_tools
    from .oc.bwtool import SCRIPT_TOOLS
    allspecs = oc_tools.specs()
    return {n: allspecs[n] for n in SCRIPT_TOOLS if n in allspecs}


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    as_json = "--json" in args
    args = [a for a in args if a != "--json"]
    if not args or args[0] in ("-h", "--help"):
        print(USAGE)
        return 0 if args else 2
    if args[0] == "wait":                 # 別の機械や列の仕事を待つ（oc/waiter.py）。道具の一式は読まない
        from .oc import waiter
        return waiter.main(args[1:])
    try:
        specs = _specs()
    except Exception as e:  # noqa: BLE001
        _out(f"Error: the L.A.P.S tools could not be loaded here: {e}", False, as_json)
        return 2
    cmd = args.pop(0)
    if cmd == "list":
        from .oc.bwtool import SCRIPT_SUMMARY
        rows = []
        for n, s in specs.items():
            what, ex = SCRIPT_SUMMARY.get(n, ((s.description or "").strip().splitlines()[0][:140] if s.description else "", ""))
            rows.append(f"- {n}: {what}" + (f"\n    e.g. {ex}" if ex else ""))
        _out("Tools you can call from a script with `bwtool <Tool> key=value ...` (prints the result; exit code 1 when the "
             "tool fails, 2 for a usage error):\n" + "\n".join(rows)
             + "\nParameters of one tool: bwtool help <Tool>. Values are read by the parameter's type: text stays as you "
             "wrote it, a list is JSON (paths='[\"/a.wav\",\"/b.wav\"]'). Add --json to get {\"ok\": ..., \"text\": ...}."
             "\nFrom Python: subprocess.run([\"bwtool\", \"WebSearch\", \"query=...\"], capture_output=True, text=True).stdout",
             True, as_json)
        return 0
    if cmd == "help":
        if not args:
            _out("Error: bwtool help <Tool>", False, as_json)
            return 2
        from .oc import tools as oc_tools
        n = oc_tools.normalize_name(args[0], specs)
        s = specs.get(n)
        if s is None:
            _out(f"Error: no tool named {args[0]!r} for scripts. Available: {', '.join(specs)}.", False, as_json)
            return 2
        props = ((s.schema or {}).get("properties") or {})
        req = set((s.schema or {}).get("required") or [])
        lines = [f"- {k}{' (required)' if k in req else ''}: {str((v or {}).get('description') or '')[:200]}"
                 for k, v in props.items()]
        _out(f"## {n}\n{s.description}\n\nParameters (pass as key=value):\n" + "\n".join(lines), True, as_json)
        return 0

    from .oc import tools as oc_tools
    name = oc_tools.normalize_name(cmd, specs)
    spec = specs.get(name)
    if spec is None:
        _out(f"Error: no tool named {cmd!r} for scripts. Available: {', '.join(specs)}. "
             "(Edit files, run commands and search files with the shell itself.)", False, as_json)
        return 2
    inp: dict = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--input":
            if i + 1 >= len(args):
                _out("Error: --input needs a JSON object (or - to read it from stdin).", False, as_json)
                return 2
            raw = sys.stdin.read() if args[i + 1] == "-" else args[i + 1]
            try:
                got = json.loads(raw)
            except ValueError as e:
                _out(f"Error: --input is not valid JSON: {e}", False, as_json)
                return 2
            if not isinstance(got, dict):
                _out("Error: --input must be a JSON object.", False, as_json)
                return 2
            inp.update(got)
            i += 2
            continue
        if "=" not in a:
            _out(f"Error: arguments are key=value (got {a!r}). Example: bwtool {name} "
                 + " ".join(f"{k}=..." for k in ((spec.schema or {}).get("required") or ["key"])), False, as_json)
            return 2
        k, v = a.split("=", 1)
        inp[k.strip()] = _Text(v)
        i += 1

    from . import policy
    mode = policy._mode()
    if mode == "readonly" and not spec.readonly:
        _out(f"Error: {name} changes things or runs programs, and this run is read-only (policy=readonly). "
             "Only read-only tools can be called from a script here.", False, as_json)
        return 2
    inp = oc_tools.normalize_input(name, inp)
    inp, moved = oc_tools.adapt_keys(spec, inp)
    inp = _typed(spec, inp)
    missing = oc_tools.missing_required(spec, inp)
    if missing:
        _out(f"Error: missing required parameter(s) {', '.join(missing)}." + oc_tools.args_help(name, spec, inp)
             + f" Example: bwtool {name} {missing[0]}=...", False, as_json)
        return 2
    inp = oc_tools.coerce_input(spec, inp)
    from .oc import bwtool as _bw
    t0 = time.time()
    try:
        res = spec.handler(inp)
        ok = bool(getattr(res, "ok", True))
        text = str(res)
    except Exception as e:  # noqa: BLE001
        ok, text = False, f"Error: {type(e).__name__}: {e}"
    secs = time.time() - t0
    _bw.record(name, ok, secs)
    if moved:
        text += "\n(Note: " + "; ".join(f"`{a}` was read as `{b}`" for a, b in moved) + ".)"
    try:
        sys.stderr.write(f"[bwtool] {name} {'ok' if ok else 'failed'} {secs:.1f}s\n")
    except Exception:
        pass
    _out(text, ok, as_json)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
