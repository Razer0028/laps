//! 音声入力（2026-10-09 オーナー「音声入力の仕組みも欲しい」）。
//!
//! 画面（🎤）が録った音を 16kHz・単声の wav（base64）で受け取り、同梱の Python で whisper.cpp にかけて文字を返す
//! （`laps.oc.voiceinput`＝聞き取りの道具 Listen と同じ本体・同じモデル・GPU があれば GPU）。声は外へ出ない。
//! 録音のファイルはデータ根の `voice-input/` に一時に置き、文字にしたら消す。文字は入力欄に入るだけ（送るのは利用者）。

use serde_json::Value;

/// 受け取る録音の上限（16kHz・16bit・単声で約 20 分ぶん）。画面は 2 分で止める。
const MAX_WAV_BYTES: usize = 40 * 1024 * 1024;

#[tauri::command]
pub async fn voice_transcribe(data: String, lang: Option<String>) -> Result<Value, String> {
    let bytes = crate::attach::b64_decode(&data)?;
    if bytes.len() < 44 {
        return Err("録音が空です".into());
    }
    if bytes.len() > MAX_WAV_BYTES {
        return Err("録音が長すぎます（2 分までにしてください）".into());
    }
    let dir = crate::app_root().join("voice-input");
    std::fs::create_dir_all(&dir).map_err(|e| format!("録音の保存先を作れません: {e}"))?;
    let stamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_millis())
        .unwrap_or(0);
    let path = dir.join(format!("rec-{stamp}.wav"));
    std::fs::write(&path, &bytes).map_err(|e| format!("録音を保存できません: {e}"))?;
    let p = path.to_string_lossy().into_owned();
    let l = lang.unwrap_or_default();
    // 初回は whisper.cpp とモデル（約 1.5GB）を取りに行くので長めに待つ。2 回目からは数秒。
    let res = crate::sidecar::run_python_module("laps.oc.voiceinput", &[&p, &l], &[], 1800).await;
    let _ = std::fs::remove_file(&path);
    let (code, out, err) = res?;
    let line = out
        .lines()
        .rev()
        .find(|s| s.trim_start().starts_with('{'))
        .ok_or_else(|| format!("聞き取りの結果を読めません（終了 {code}）: {}", err.lines().last().unwrap_or("").trim()))?;
    let v: Value = serde_json::from_str(line).map_err(|e| format!("聞き取りの結果を読めません: {e}"))?;
    if v.get("ok").and_then(Value::as_bool) != Some(true) {
        return Err(v.get("error").and_then(Value::as_str).unwrap_or("聞き取りに失敗しました").to_string());
    }
    Ok(v)
}
