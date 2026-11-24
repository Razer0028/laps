; L.A.P.S Console — インストーラの前処理
;
; 「使用中のファイルは置き換えられない」を避けるため、更新・入れ直しの瞬間だけ関連プロセスを止める。
;   1. llama-server.exe が resources\llama\*.dll を掴んだまま残っていることがある
;      （このアプリは「閉じても推論器は生き続ける」設計）。
;   2. blackwall-console.exe 自身が動いたまま。
;   3. 同梱の Python（pyruntime）で動いている本体。
; 推論器は状態を持たないので、止めても次にモデルを使うときに読み直すだけで済む。

!macro NSIS_HOOK_PREINSTALL
  DetailPrint "使用中のプロセスを停止しています..."

  ; 推論器（router mode ではモデルごとに別プロセスが居るので /T で子ごと）
  nsExec::ExecToLog 'taskkill /F /T /IM llama-server.exe'
  Pop $0

  ; アプリ本体
  nsExec::ExecToLog 'taskkill /F /IM blackwall-console.exe'
  Pop $0

  ; 同梱の Python で動いている本体。★名前で撃たない（利用者自身の Python を巻き込む）。置き場（インストール先）で絞る。
  nsExec::ExecToLog `powershell -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-Process python,pythonw -ErrorAction SilentlyContinue | Where-Object { $$_.Path -and ($$_.Path -like '$INSTDIR\pyruntime\*') } | Stop-Process -Force"`
  Pop $0

  ; ハンドルが解放されるまでの猶予。
  Sleep 1500
!macroend

!macro NSIS_HOOK_POSTINSTALL
  DetailPrint "完了しました。推論器は次にモデルを使うときに起動します。"
!macroend
