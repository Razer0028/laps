//! Windows の通知（ほかのデバイスのチャットが終わった・聞かれた）。押されたら窓を前に出し、画面へ `peer-open` で知らせる。
//!
//! ★2026-11-17: 音声アシスタントの着信（応答・拒否のボタンつきの通知）は、オーナー「あんま使ってないので音声アシスタントは廃止」で外した。
//! ★新しい部品は足さない: tauri-winrt-notification ではなく、既に使っている windows で直接出す。
//! ★名乗り（AppUserModelID）はアプリの識別名（tauri.conf.json の identifier）。インストーラがスタートメニューの
//!   ショートカットに同じ名乗りを付けている（SetLnkAppUserModelId）ので、通知はアプリの名前で出る。
use tauri::AppHandle;

#[cfg(windows)]
mod imp {
    use serde_json::json;
    use tauri::{AppHandle, Emitter, Manager};
    use windows::core::{IInspectable, HSTRING};
    use windows::Data::Xml::Dom::XmlDocument;
    use windows::Foundation::TypedEventHandler;
    use windows::UI::Notifications::{ToastNotification, ToastNotificationManager};

    fn esc(s: &str) -> String {
        s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;").replace('"', "&quot;")
    }

    fn front(app: &AppHandle) {
        if let Some(w) = app.get_webview_window("main") {
            let _ = w.unminimize();
            let _ = w.show();
            let _ = w.set_focus();
        }
    }

    /// ふつうの知らせ（2026-10-30・ほかの端末の会話が終わった・聞かれた）。押されたら窓を前に出し、画面へ
    /// `peer-open`（{key}）で知らせる。音は Windows の既定（着信と違ってアプリは鳴らさない）。
    pub fn note(app: &AppHandle, title: &str, body: &str, key: &str) -> Result<(), String> {
        let aumid = app.config().identifier.clone();
        let xml = format!(
            "<toast launch=\"{}\"><visual><binding template=\"ToastGeneric\"><text>{}</text><text>{}</text></binding></visual></toast>",
            esc(key), esc(title), esc(body)
        );
        let doc = XmlDocument::new().map_err(|e| format!("通知を作れません: {e}"))?;
        doc.LoadXml(&HSTRING::from(xml)).map_err(|e| format!("通知の中身を読めません: {e}"))?;
        let toast = ToastNotification::CreateToastNotification(&doc).map_err(|e| format!("通知を作れません: {e}"))?;
        let a1 = app.clone();
        let k = key.to_string();
        toast
            .Activated(&TypedEventHandler::<ToastNotification, IInspectable>::new(move |_, _| {
                front(&a1);
                let _ = a1.emit("peer-open", json!({ "key": k }));
                Ok(())
            }))
            .map_err(|e| format!("通知の応答を受け取れません: {e}"))?;
        let notifier = ToastNotificationManager::CreateToastNotifierWithId(&HSTRING::from(aumid))
            .map_err(|e| format!("通知を出せません: {e}"))?;
        notifier.Show(&toast).map_err(|e| format!("通知を出せません: {e}"))
    }
}

/// ふつうの知らせ（Windows だけ・ほかでは何もしない）。WinRT は別の糸で呼ぶ。
pub fn note(app: &AppHandle, title: String, body: String, key: String) {
    #[cfg(windows)]
    {
        let app = app.clone();
        let t: String = title.chars().take(80).collect();
        let b: String = body.chars().take(200).collect();
        tauri::async_runtime::spawn_blocking(move || {
            let _ = imp::note(&app, &t, &b, &key);
        });
    }
    #[cfg(not(windows))]
    {
        let _ = (app, title, body, key);
    }
}
