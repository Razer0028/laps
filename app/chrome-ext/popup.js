const $ = (id) => document.getElementById(id);
const LABEL = { connected: "つながっています", disconnected: "つながっていません（L.A.P.S が動いているか確かめてください）",
  "no-token": "合言葉を入れてください", denied: "合言葉が違います", error: "つなげません" };
async function paint(s) {
  const st = $("st");
  st.textContent = (LABEL[s && s.status] || "確かめています…") + (s && s.detail && s.status !== "connected" ? `（${s.detail}）` : "");
  st.classList.toggle("on", !!s && s.status === "connected");
}
async function load() {
  const v = await chrome.storage.local.get(["port", "token"]);
  $("port").value = v.port || 18765;
  $("token").value = v.token || "";
  paint(await chrome.runtime.sendMessage({ type: "status" }));
}
$("save").addEventListener("click", async () => {
  await chrome.storage.local.set({ port: Number($("port").value || 18765), token: $("token").value.trim() });
  setTimeout(async () => paint(await chrome.runtime.sendMessage({ type: "reconnect" })), 600);
});
load();
