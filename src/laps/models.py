"""推論機の**銘柄表** — 複数のモデルを名前で切り替える。

★オーナー指示（2026-09-17）「マルチモデル互換にして欲しい。kimi とか使ってみたい」。

★いままで何が足りなかったか（実装を読んで確かめた）:
  ① 名前が無い。`LAPS_BACKEND` / `_MODEL` / `_N_CTX` / `_NATIVE_TOOLS` を
     **別々に・整合させて**渡す必要があり、1 つ忘れると黙って混ざる。
  ② 送る調整値が **llama.cpp 方言**（`repeat_penalty` / `repeat_last_n` / `top_k`）。
     llama.cpp は未知のキーを黙って捨てるので害が無かったが、**厳しい API は 400 を返す**。
  ③ 窓の大きさがモデルと結び付いていない。`LAPS_N_CTX` を忘れると
     **32,768 のつもりで 128K のモデルを使う**（逆もある）。窓の見積りは既に
     最大 9,168 トークンずれているので、ここを推測に任せてはいけない。
  ④ 道具の呼び方（native / text）がモデル側の対応と結び付いていない。

★**外に出る銘柄は fail-closed**（`external=True`）。
  エージェントは作業場のファイルを読んで**そのまま文脈に載せる**。
  外部の API を選ぶことは、**その中身を外へ送ること**と同じ。
  だから `LAPS_ALLOW_EXTERNAL=1` を明示しない限り選べない。
  ★これは「使うな」ではなく「**知らずに使わない**」ための門。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Profile:
    """1 つの銘柄。**モデルと窓と方言は一緒に決まる**ので、まとめて持つ。"""

    name: str
    base_url: str
    model: str
    n_ctx: int
    #: 調整値の方言。"llamacpp" は独自キーを足してよい。"openai" は標準キーだけ。
    dialect: str = "openai"
    #: 道具の呼び方。True なら推論機側のテンプレートが道具を注入する。
    native_tools: bool = True
    #: 鍵を読む環境変数の名前（値そのものは**絶対に持たない**）。
    key_env: str = "LAPS_API_KEY"
    #: ラボの外へ出るか。True なら明示の許可が要る。
    external: bool = False
    notes: str = ""
    #: 既定の調整値（方言に合うものだけ）。
    sampling: dict = field(default_factory=dict)

    def api_key(self) -> str:
        return os.environ.get(self.key_env, "") or "sk-no-key-required"


#: ★ここに無い銘柄は使えない。増やすときは**窓と方言も一緒に**書くこと。
PROFILES: dict[str, Profile] = {
    # -- ラボの中 ---------------------------------------------------------
    "local-coder": Profile(
        name="local-coder",
        base_url="http://127.0.0.1:8080/v1",
        model="default",
        n_ctx=32_768,
        dialect="llamacpp",
        native_tools=True,
        external=False,
        # ★2026-09-20 オーナー決定で **推論機の中身を Qwen3.6-35B-A3B（abliterated・Q4_K_M）へ**。
        #   別名 `blackwall-coder` と口（推論機:8741）は**そのまま**＝出荷済みの実機は何も変えずに繋がる。
        #   実際にどの GGUF が載っているかは推論機の `/props` の model_path が正（ここに書くと古くなる）。
        #   構造: 40 層中 全注意は 10 層だけ（残りは線形注意）＝KV が極小（32K で 0.67GB・131K で 2.68GB）。
        #   公式の公表値（思考 on・256K 窓・3h 上限）: SWE-bench Verified 73.4 / Terminal-Bench 2.0 51.5。
        #   ⚠ うちは**思考 off・32K 窓・900 秒**で回すので、この数字はそのまま出ない。
        notes="推論機 推論機 の常駐モデル（2026-09-20〜 Qwen3.6-35B-A3B abliterated Q4_K_M）",
        # ★値は backend.py の既定と同じ。Coder のモデルカード推奨。
        sampling={"top_p": 0.8, "top_k": 20, "repeat_penalty": 1.05},
    ),
    # -- 外に出るもの（★明示の許可が要る）---------------------------------
    "kimi-k2": Profile(
        name="kimi-k2",
        base_url="https://api.moonshot.ai/v1",
        model="kimi-k2-0905-preview",
        n_ctx=256_000,
        dialect="openai",
        native_tools=True,
        key_env="MOONSHOT_API_KEY",
        external=True,
        notes="Moonshot の Kimi K2（道具呼び出し向き）。★作業場の中身が外へ出る",
        sampling={"top_p": 0.9},          # ★独自キーを送らない（400 になる）
    ),
    "kimi-k2-turbo": Profile(
        name="kimi-k2-turbo",
        base_url="https://api.moonshot.ai/v1",
        model="kimi-k2-turbo-preview",
        n_ctx=256_000,
        dialect="openai",
        native_tools=True,
        key_env="MOONSHOT_API_KEY",
        external=True,
        notes="同上の高速版",
        sampling={"top_p": 0.9},
    ),
}

DEFAULT_PROFILE = "local-coder"


class NotAllowed(RuntimeError):
    """外に出る銘柄を、許可なしに選んだ。"""


def get(name: str | None = None) -> Profile:
    """銘柄を引く。**外に出るものは許可が無ければ断る**（fail-closed）。"""
    key = (name or os.environ.get("LAPS_PROFILE") or DEFAULT_PROFILE).strip()
    p = PROFILES.get(key)
    if p is None:
        raise KeyError(f"知らない銘柄です: {key}（使えるのは {', '.join(sorted(PROFILES))}）")
    if p.external and os.environ.get("LAPS_ALLOW_EXTERNAL") != "1":
        raise NotAllowed(
            f"`{p.name}` はラボの外（{p.base_url}）へ出ます。\n"
            "  エージェントは作業場のファイルを読んで、そのまま文脈に載せます。\n"
            "  ★つまり **その中身が外部の事業者へ送られます**。\n"
            f"  承知のうえで使うなら LAPS_ALLOW_EXTERNAL=1 と {p.key_env} を渡してください。")
    if p.external and not os.environ.get(p.key_env):
        raise NotAllowed(f"`{p.name}` には {p.key_env} が要ります（値はここには持ちません）")
    return p


def sampling_for(p: Profile) -> dict:
    """その銘柄に**送ってよい**調整値だけを返す。

    ★llama.cpp は未知のキーを黙って捨てるので今まで害が無かったが、
      厳しい API は 400 を返す。方言を跨いで同じ辞書を送らないこと。
    """
    out = dict(p.sampling)
    if p.dialect != "llamacpp":
        for k in ("repeat_penalty", "repetition_penalty", "repeat_last_n", "top_k",
                  "min_p", "typical_p", "tfs_z", "mirostat"):
            out.pop(k, None)
    return out


def describe() -> str:
    """`/model` などで人に見せる一覧。★鍵の値は出さない。"""
    rows = []
    for k in sorted(PROFILES):
        p = PROFILES[k]
        mark = "🌐外部" if p.external else "🏠ラボ内"
        ok = ""
        if p.external:
            ok = " ✅許可済" if os.environ.get("LAPS_ALLOW_EXTERNAL") == "1" else " ⛔要許可"
            ok += "・鍵あり" if os.environ.get(p.key_env) else "・鍵なし"
        rows.append(f"  {k:<16} {mark}{ok}  窓 {p.n_ctx:,}  {p.notes}")
    return "使える銘柄:\n" + "\n".join(rows)
