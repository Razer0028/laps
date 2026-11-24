"""PowerShell の `\\$` を素の `$` に戻す（★オーナー実機の実エラー・2026-09-21）。

実機（`.254`）で出ていたもの:

    [exit=1] 発生場所 …\\bwcmd-bez4rxk0.ps1:8 文字:28
    +     Write-Host ('{0:X2}' -f \\$bytes[\\$i]) -NoNewline
    +                            ~
    '-f' 演算子に続けて値の式を指定する必要があります。

★原因: `powershell -Command "…"` の**包みを外して .ps1 に書く**とき、
  引用符の逃がし（`\\"`）は戻していたのに **`\\$` を戻していなかった**。
★モデルは cmd.exe 向けの逃がしのつもりで `\\$` と書く。しかし
  **PowerShell の逃がしはバッククォートであって円記号ではない**ので、
  `\\$bytes` は「文字の \\ ＋ 変数 $bytes」と読まれ、式として壊れる。
"""
from laps.tools.bash import _unwrap_multiline_powershell as unwrap


def _wrap(body: str) -> str:
    return 'powershell -NoProfile -Command "\n' + body + '\n"'


def test_the_real_failure_from_the_owners_machine():
    out = unwrap(_wrap(r"""$b = [IO.File]::ReadAllBytes('a.bin')
for (\$i=0; \$i -lt 4; \$i++) {
  Write-Host ('{0:X2}' -f \$bytes[\$i]) -NoNewline
}"""))
    assert out is not None
    assert "\\$" not in out, f"\\$ が残っている: {out!r}"
    assert "$i" in out and "$bytes" in out


def test_quotes_are_still_unescaped():
    """既に直していた分を壊さない。"""
    out = unwrap(_wrap(r'Write-Host \"hello\"'))
    assert out == 'Write-Host "hello"'


def test_a_real_backslash_before_dollar_is_kept():
    """★`\\\\$` は「円記号を出したい」形。これを壊すと別の事故になる。"""
    out = unwrap(_wrap(r"Write-Host 'C:\\$x'"))
    assert r"\\$" in out, f"円記号まで食べている: {out!r}"


def test_single_line_commands_are_untouched():
    """1 行なら包みを外さない（実機 0.1.232 で 15/15 通っていた経路）。"""
    assert unwrap('powershell -Command "Get-Date"') is None
