fn main() {
    // tauri.conf.json を読んで Context を生成し、Windows のリソース（アイコン・マニフェスト）を
    // 埋め込む。これが無いと generate_context!() が解決できずビルドが通らない。
    tauri_build::build()
}
