//! GPU の検出。
//!
//! 判定に使うのは「総 VRAM」ではなく **空き / 予算** である。デスクトップ合成（DWM）と
//! 既に動いているアプリが常に VRAM を握っているため、総量で載る載らないを言うと必ず外す。
//!
//! ⛔ WMI `Win32_VideoController.AdapterRAM` は絶対に読まない。
//!    MOF 定義が `uint32` なので約 4GB で頭打ちになるのは**構造的な性質**であり、
//!    ドライバやバージョンの問題ではない（12GB の 4070 が 4GB と報告される正体）。
//!    修正する方法は無いので、経路ごと使わない。
//!
//! 優先順位:
//!   1. DXGI `IDXGIAdapter3::QueryVideoMemoryInfo`（ベンダー非依存・OS が示す予算）
//!   2. NVIDIA なら NVML v2（`reserved` を明示的に持つのは v2 のみ）で空きを取り直す
//!   3. NVML が無ければ `nvidia-smi.exe` を実行して読む
//!
//! ⚠ `nvml.dll` / `nvidia-smi.exe` は PATH に無い。DCH ドライバは System32、
//!    旧来のインストールは `%ProgramW6432%\NVIDIA Corporation\NVSMI\` に置く。
//!    **フルパスで解決する**こと。

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GpuInfo {
    pub name: String,
    /// 判定の主軸ではない。DXGI の Budget（OS が今このプロセスに許す量）を入れる。
    pub vram_total_mb: u64,
    /// 実際に空いている量。**fit 判定はこちらで行う。**
    pub vram_free_mb: u64,
    /// どの経路で得た値か（UI が出所として表示する）。
    pub source: String,
    /// 代替経路に落ちた、総量しか取れなかった等、値の信頼度に関わる注記。
    pub warnings: Vec<String>,
}

const BYTES_PER_MB: u64 = 1024 * 1024;

#[inline]
#[cfg_attr(not(windows), allow(dead_code))]
fn to_mb(bytes: u64) -> u64 {
    bytes / BYTES_PER_MB
}

/// GPU を調べる。
///
/// DXGI も NVML もブロッキングかつ COM ポインタを扱うため、
/// **すべて `spawn_blocking` の中に閉じ込め、await をまたいで COM を持ち出さない**。
/// 戻り値（`GpuInfo`）は素のデータなので Send 制約で困らない。
#[tauri::command]
pub async fn probe_gpu() -> Result<GpuInfo, String> {
    tokio::task::spawn_blocking(probe_gpu_blocking)
        .await
        .map_err(|e| format!("GPU の検出タスクが異常終了しました: {e}"))?
}

// ---------------------------------------------------------------------------
// Windows 実装
// ---------------------------------------------------------------------------

#[cfg(windows)]
fn probe_gpu_blocking() -> Result<GpuInfo, String> {
    let mut warnings: Vec<String> = Vec::new();

    let dxgi = win::dxgi_best_adapter().map_err(|e| {
        format!("GPU を検出できませんでした（DXGI）: {e}。ドライバが入っているか確認してください。")
    })?;

    let mut info = GpuInfo {
        name: dxgi.name.clone(),
        vram_total_mb: to_mb(dxgi.budget_bytes.max(dxgi.dedicated_bytes)),
        vram_free_mb: to_mb(dxgi.budget_bytes.saturating_sub(dxgi.current_usage_bytes)),
        source: "DXGI QueryVideoMemoryInfo".to_string(),
        warnings: Vec::new(),
    };

    if dxgi.budget_bytes == 0 {
        // IDXGIAdapter3 が取れない（極めて古い OS / 変わったドライバ）場合。
        // 総量しか無いと空きが分からないので、その旨を明示して総量を空きに流用しない。
        info.vram_total_mb = to_mb(dxgi.dedicated_bytes);
        info.vram_free_mb = 0;
        info.source = "DXGI GetDesc1 (総量のみ)".to_string();
        warnings.push(
            "空き VRAM を取得できませんでした（IDXGIAdapter3 が使えません）。総量のみ表示しています。"
                .to_string(),
        );
    } else if dxgi.dedicated_bytes > dxgi.budget_bytes {
        // Budget は「今このプロセスが使ってよい量」であって物理搭載量ではない。
        // 他のアプリが握っていると搭載量より小さく出るのが正常なので、誤解されないよう注記する。
        warnings.push(format!(
            "OS が示す予算は {} MB で、搭載量 {} MB より小さくなっています（他のアプリが VRAM を使用中）。",
            to_mb(dxgi.budget_bytes),
            to_mb(dxgi.dedicated_bytes)
        ));
    }

    // NVIDIA なら NVML で空きを取り直す。DXGI の Budget はプロセス視点の予算であり、
    // NVML の free は GPU 全体の実測なので、モデルが載るかの判断にはこちらが近い。
    const VENDOR_NVIDIA: u32 = 0x10DE;
    if dxgi.vendor_id == VENDOR_NVIDIA {
        match win::nvml_memory() {
            Ok(Some(m)) => {
                info.vram_total_mb = to_mb(m.total);
                info.vram_free_mb = to_mb(m.free);
                info.source = format!("NVML {} (DXGI で機種特定)", m.api);
            }
            Ok(None) | Err(_) => {
                // NVML が無い/初期化できない → nvidia-smi にフォールバック。
                match win::nvidia_smi_memory() {
                    Ok(Some((total_mb, free_mb))) => {
                        info.vram_total_mb = total_mb;
                        info.vram_free_mb = free_mb;
                        info.source = "nvidia-smi.exe".to_string();
                        warnings.push(
                            "NVML を読み込めなかったため nvidia-smi の値を使いました。".to_string(),
                        );
                    }
                    _ => {
                        warnings.push(
                            "NVIDIA GPU ですが NVML も nvidia-smi も使えませんでした。DXGI の予算値を表示しています。"
                                .to_string(),
                        );
                    }
                }
            }
        }
    }

    info.warnings = warnings;
    Ok(info)
}

#[cfg(windows)]
mod win {
    use std::ffi::c_void;
    use std::os::windows::process::CommandExt;
    use std::process::Command;

    use windows::core::{Interface, PCSTR, PCWSTR};
    use windows::Win32::Foundation::HMODULE;
    use windows::Win32::Graphics::Dxgi::{
        CreateDXGIFactory1, IDXGIAdapter1, IDXGIAdapter3, IDXGIFactory1, DXGI_ADAPTER_DESC1,
        DXGI_MEMORY_SEGMENT_GROUP_LOCAL, DXGI_QUERY_VIDEO_MEMORY_INFO,
    };
    // FreeLibrary は Foundation にある（LibraryLoader ではない）。windows 0.58 で確認。
    use windows::Win32::Foundation::FreeLibrary;
    use windows::Win32::System::LibraryLoader::{GetProcAddress, LoadLibraryW};

    /// `DXGI_ADAPTER_FLAG_SOFTWARE` の値。定数を直書きしているのは、
    /// windows crate の版によって `Flags` が u32 だったり newtype だったりして
    /// 型名で書くと版差でコンパイルが割れるため。値そのものは DXGI の ABI で固定。
    const DXGI_ADAPTER_FLAG_SOFTWARE_BIT: u32 = 2;

    pub struct DxgiAdapter {
        pub name: String,
        pub vendor_id: u32,
        pub dedicated_bytes: u64,
        pub budget_bytes: u64,
        pub current_usage_bytes: u64,
    }

    fn desc_name(desc: &DXGI_ADAPTER_DESC1) -> String {
        let end = desc
            .Description
            .iter()
            .position(|&c| c == 0)
            .unwrap_or(desc.Description.len());
        String::from_utf16_lossy(&desc.Description[..end])
    }

    /// 物理アダプタのうち DedicatedVideoMemory が最大のものを選ぶ。
    /// ソフトウェアアダプタ（Microsoft Basic Render Driver / WARP）は除外する。
    /// これらは VRAM 0 を返すので値としても無害だが、名前が UI に出ると混乱するため明示的に外す。
    pub fn dxgi_best_adapter() -> Result<DxgiAdapter, String> {
        let factory: IDXGIFactory1 =
            unsafe { CreateDXGIFactory1() }.map_err(|e| format!("CreateDXGIFactory1: {e}"))?;

        let mut best: Option<DxgiAdapter> = None;
        let mut index: u32 = 0;

        loop {
            // 列挙は DXGI_ERROR_NOT_FOUND で終わる。エラー種別で分岐せず、
            // 失敗＝末尾として扱う（他のエラーでも列挙を続ける意味は無い）。
            let adapter: IDXGIAdapter1 = match unsafe { factory.EnumAdapters1(index) } {
                Ok(a) => a,
                Err(_) => break,
            };
            index += 1;

            // windows 0.58 の GetDesc1 は引数を取らず Result<DXGI_ADAPTER_DESC1> を返す
            // （出力引数を渡す形は旧版）。版を上げるときはここも確認する。
            let desc: DXGI_ADAPTER_DESC1 = match unsafe { adapter.GetDesc1() } {
                Ok(d) => d,
                Err(_) => continue,
            };

            let name = desc_name(&desc);
            let is_software = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE_BIT) != 0
                || name.contains("Basic Render Driver");
            if is_software {
                continue;
            }

            let dedicated_bytes = desc.DedicatedVideoMemory as u64;

            // IDXGIAdapter3 は Windows 10 以降。取れなければ総量だけで続行する。
            let (budget_bytes, current_usage_bytes) = match adapter.cast::<IDXGIAdapter3>() {
                Ok(a3) => {
                    let mut vmi = DXGI_QUERY_VIDEO_MEMORY_INFO::default();
                    // node 0 / LOCAL セグメント＝ディスクリート GPU の VRAM 本体。
                    // NON_LOCAL は共有システムメモリなので fit 判定には使わない。
                    match unsafe {
                        a3.QueryVideoMemoryInfo(0, DXGI_MEMORY_SEGMENT_GROUP_LOCAL, &mut vmi)
                    } {
                        Ok(()) => (vmi.Budget, vmi.CurrentUsage),
                        Err(_) => (0, 0),
                    }
                }
                Err(_) => (0, 0),
            };

            let candidate = DxgiAdapter {
                name,
                vendor_id: desc.VendorId,
                dedicated_bytes,
                budget_bytes,
                current_usage_bytes,
            };

            let replace = match &best {
                None => true,
                Some(b) => candidate.dedicated_bytes > b.dedicated_bytes,
            };
            if replace {
                best = Some(candidate);
            }
        }

        best.ok_or_else(|| "利用できる GPU アダプタが見つかりませんでした".to_string())
    }

    // -- NVML -----------------------------------------------------------------

    pub struct NvmlMemory {
        pub total: u64,
        pub free: u64,
        /// "v2" か "v1"。どちらで読めたかを出所として UI に出す。
        pub api: &'static str,
    }

    type NvmlReturn = i32;
    const NVML_SUCCESS: NvmlReturn = 0;

    type FnNvmlInitV2 = unsafe extern "C" fn() -> NvmlReturn;
    type FnNvmlShutdown = unsafe extern "C" fn() -> NvmlReturn;
    type FnNvmlDeviceGetHandleByIndexV2 =
        unsafe extern "C" fn(u32, *mut *mut c_void) -> NvmlReturn;
    type FnNvmlDeviceGetMemoryInfoV2 =
        unsafe extern "C" fn(*mut c_void, *mut NvmlMemoryV2) -> NvmlReturn;
    type FnNvmlDeviceGetMemoryInfo = unsafe extern "C" fn(*mut c_void, *mut NvmlMemoryV1)
        -> NvmlReturn;

    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    struct NvmlMemoryV1 {
        total: u64,
        free: u64,
        used: u64,
    }

    /// `nvmlMemory_v2_t`。v1 と違い `reserved`（ドライバ予約分）を持つので、
    /// v1 の free（reserved を含んだまま）より実際に使える量に近い。
    #[repr(C)]
    #[derive(Default, Clone, Copy)]
    struct NvmlMemoryV2 {
        version: u32,
        total: u64,
        reserved: u64,
        free: u64,
        used: u64,
    }

    /// NVML の構造体バージョン: `sizeof(struct) | (version << 24)`。
    /// サイズを実際の型から取るので、パディングの取り違えが起きない。
    fn nvml_memory_v2_version() -> u32 {
        (std::mem::size_of::<NvmlMemoryV2>() as u32) | (2u32 << 24)
    }

    fn wide(s: &str) -> Vec<u16> {
        s.encode_utf16().chain(std::iter::once(0)).collect()
    }

    /// nvml.dll / nvidia-smi.exe の候補パス。**PATH に無いのでフルパスで探す。**
    /// System32 が先（DCH ドライバ）、次に旧来の NVSMI ディレクトリ。
    fn nvidia_candidates(file: &str) -> Vec<String> {
        let mut out = Vec::new();
        if let Ok(root) = std::env::var("SystemRoot") {
            out.push(format!("{root}\\System32\\{file}"));
        }
        // 64bit プロセスからは ProgramW6432 が「本物の」Program Files を指す。
        for var in ["ProgramW6432", "ProgramFiles"] {
            if let Ok(pf) = std::env::var(var) {
                out.push(format!("{pf}\\NVIDIA Corporation\\NVSMI\\{file}"));
            }
        }
        out
    }

    struct Library(HMODULE);

    impl Library {
        fn load(path: &str) -> Option<Library> {
            let w = wide(path);
            // LoadLibraryW は失敗を Err で返すので、ここでの成否判定はこれで足りる。
            let h = unsafe { LoadLibraryW(PCWSTR(w.as_ptr())) }.ok()?;
            Some(Library(h))
        }

        /// 名前は NUL 終端で渡すこと（呼び出し側で "\0" を付ける）。
        fn proc(&self, name: &[u8]) -> Option<unsafe extern "system" fn() -> isize> {
            debug_assert!(name.last() == Some(&0));
            unsafe { GetProcAddress(self.0, PCSTR(name.as_ptr())) }
        }
    }

    impl Drop for Library {
        fn drop(&mut self) {
            let _ = unsafe { FreeLibrary(self.0) };
        }
    }

    /// NVML から 0 番デバイスのメモリを読む。
    /// `Ok(None)` は「NVML が使えなかった」＝呼び出し側でフォールバックする合図。
    pub fn nvml_memory() -> Result<Option<NvmlMemory>, String> {
        let lib = match nvidia_candidates("nvml.dll")
            .into_iter()
            .find_map(|p| Library::load(&p))
        {
            Some(l) => l,
            None => return Ok(None),
        };

        // 必須シンボル。1つでも欠けたら NVML は使えないものとして扱う。
        let (init, shutdown, get_handle) = match (
            lib.proc(b"nvmlInit_v2\0"),
            lib.proc(b"nvmlShutdown\0"),
            lib.proc(b"nvmlDeviceGetHandleByIndex_v2\0"),
        ) {
            (Some(a), Some(b), Some(c)) => (a, b, c),
            _ => return Ok(None),
        };

        let init: FnNvmlInitV2 = unsafe { std::mem::transmute(init) };
        let shutdown: FnNvmlShutdown = unsafe { std::mem::transmute(shutdown) };
        let get_handle: FnNvmlDeviceGetHandleByIndexV2 = unsafe { std::mem::transmute(get_handle) };

        if unsafe { init() } != NVML_SUCCESS {
            return Ok(None);
        }

        // ここから先は必ず nvmlShutdown を通す（早期 return しない）。
        let mut result: Option<NvmlMemory> = None;
        let mut device: *mut c_void = std::ptr::null_mut();
        if unsafe { get_handle(0, &mut device) } == NVML_SUCCESS && !device.is_null() {
            // まず v2。古いドライバには存在しない/エラーを返すので v1 に落ちる。
            if let Some(sym) = lib.proc(b"nvmlDeviceGetMemoryInfo_v2\0") {
                let f: FnNvmlDeviceGetMemoryInfoV2 = unsafe { std::mem::transmute(sym) };
                let mut m = NvmlMemoryV2::default();
                m.version = nvml_memory_v2_version();
                if unsafe { f(device, &mut m) } == NVML_SUCCESS && m.total > 0 {
                    result = Some(NvmlMemory {
                        total: m.total,
                        free: m.free,
                        api: "v2",
                    });
                }
            }
            if result.is_none() {
                if let Some(sym) = lib.proc(b"nvmlDeviceGetMemoryInfo\0") {
                    let f: FnNvmlDeviceGetMemoryInfo = unsafe { std::mem::transmute(sym) };
                    let mut m = NvmlMemoryV1::default();
                    if unsafe { f(device, &mut m) } == NVML_SUCCESS && m.total > 0 {
                        result = Some(NvmlMemory {
                            total: m.total,
                            free: m.free,
                            api: "v1",
                        });
                    }
                }
            }
        }

        unsafe { shutdown() };
        Ok(result)
    }

    // -- nvidia-smi -----------------------------------------------------------

    const CREATE_NO_WINDOW: u32 = 0x0800_0000;

    /// `nvidia-smi.exe` から (total_mb, free_mb) を取る。
    /// GUI アプリなのでコンソール窓が一瞬出ないよう CREATE_NO_WINDOW を付ける。
    pub fn nvidia_smi_memory() -> Result<Option<(u64, u64)>, String> {
        let exe = match nvidia_candidates("nvidia-smi.exe")
            .into_iter()
            .find(|p| std::path::Path::new(p).exists())
        {
            Some(p) => p,
            None => return Ok(None),
        };

        let out = Command::new(exe)
            .args([
                "--query-gpu=memory.total,memory.free",
                "--format=csv,noheader,nounits",
            ])
            .creation_flags(CREATE_NO_WINDOW)
            .output()
            .map_err(|e| format!("nvidia-smi の実行に失敗しました: {e}"))?;

        if !out.status.success() {
            return Ok(None);
        }

        // 出力例: "12288, 10240"（複数 GPU なら行が並ぶ。先頭行だけ使う）
        let text = String::from_utf8_lossy(&out.stdout);
        let line = match text.lines().find(|l| !l.trim().is_empty()) {
            Some(l) => l,
            None => return Ok(None),
        };
        let mut it = line.split(',').map(|s| s.trim());
        let total = it.next().and_then(|s| s.parse::<u64>().ok());
        let free = it.next().and_then(|s| s.parse::<u64>().ok());
        match (total, free) {
            (Some(t), Some(f)) => Ok(Some((t, f))),
            _ => Ok(None),
        }
    }
}

// ---------------------------------------------------------------------------
// 非 Windows（Linux 上での型検査・レビュー用のスタブ）
// ---------------------------------------------------------------------------

#[cfg(not(windows))]
fn probe_gpu_blocking() -> Result<GpuInfo, String> {
    // 本アプリは Windows 専用。ここは「.9 の Linux でツリーを型検査できるようにする」ためだけの実装で、
    // 値を推測して返すことはしない（0 と明示のラベルを返す）。
    Ok(GpuInfo {
        name: "unknown".to_string(),
        vram_total_mb: 0,
        vram_free_mb: 0,
        source: "unavailable (non-Windows)".to_string(),
        warnings: vec![
            "この実行環境は Windows ではありません。GPU の検出は Windows でのみ動作します。"
                .to_string(),
        ],
    })
}
