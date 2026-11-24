// bw-desktop: the Desktop tool's helper for Blackwall (Windows only).
//
// The model reads text only, so the screen is given to it as the UI Automation
// tree: one numbered line per control. The model acts on those numbers.
//
// Protocol: one JSON object per line on stdin, one JSON object per line on
// stdout: {"ok":true|false,"text":"..."}. Output is ASCII only (everything
// else is \u-escaped), so the console code page never matters.
//
// This file is compiled on the user's machine by the csc.exe of the .NET
// Framework 4 that ships with Windows (C# 5). Keep the syntax old: no $"",
// no ?., no nameof, no out var, no expression-bodied members.
// Keep this file ASCII only (comments too).
//
// OCR (Windows.Media.Ocr) is compiled in only with /define:BW_OCR, which needs the WinRT
// metadata in System32\WinMetadata. If that build fails, the helper is built without it and
// the ocr action says so.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
#if BW_OCR
using Windows.Graphics.Imaging;
using Windows.Media.Ocr;
using Windows.Storage.Streams;
#endif

namespace BwDesktop
{
    [StructLayout(LayoutKind.Sequential)]
    struct MOUSEINPUT { public int dx; public int dy; public uint mouseData; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Sequential)]
    struct KEYBDINPUT { public ushort wVk; public ushort wScan; public uint dwFlags; public uint time; public IntPtr dwExtraInfo; }

    [StructLayout(LayoutKind.Explicit)]
    struct INPUTUNION { [FieldOffset(0)] public MOUSEINPUT mi; [FieldOffset(0)] public KEYBDINPUT ki; }

    [StructLayout(LayoutKind.Sequential)]
    struct INPUT { public uint type; public INPUTUNION u; }

    [StructLayout(LayoutKind.Sequential)]
    struct POINT { public int X; public int Y; }

    [StructLayout(LayoutKind.Sequential)]
    struct RECT { public int Left; public int Top; public int Right; public int Bottom; }

    delegate bool EnumWindowsProc(IntPtr hwnd, IntPtr lParam);

    [StructLayout(LayoutKind.Sequential)]
    struct MONITORINFO { public int cbSize; public RECT rcMonitor; public RECT rcWork; public uint dwFlags; }

    delegate bool MonitorEnumProc(IntPtr hMonitor, IntPtr hdc, ref RECT rect, IntPtr data);

#if BW_OVERLAY
    // ---- 操作中の表示（2026-11-18 オーナー「L.A.P.S の Computer Use は、操作されているのか・どこを操作しているのかが分かりづらい。
    //      Claude は画面の端が光ってカーソルも変わるので分かりやすい」）------------------------------------------------
    // 画面全体（全モニタ）を覆う最前面の透明な窓。クリックを通す・タスクバーに出ない・フォーカスを取らない。
    //   縁の光（L.A.P.S の赤→青）・押す場所の印と波紋・右下の札（何をしているか・Ctrl+Alt+Q で止める）。
    //   光の脈は窓の透明度だけで動かす（描き直さない＝軽い）。6 秒動きが無ければ消える。
    //   Ctrl+Alt+Q（表示が出ている間だけ・L.A.P.S 自身が送ったキーは数えない・押したキーは画面に渡さない）で止める＝次の操作が「利用者が止めた」で返る。
    //   写真（look・ocr）を撮る間は隠す（モデルが表示を画面の一部と読み違えないように）。
    static class Overlay
    {
        static System.Threading.Thread thread;
        static OverlayForm form;
        static readonly object gate = new object();
        static volatile bool stop;
        public static bool Enabled = true;

        public static bool TakeStop() { bool s = stop; stop = false; return s; }
        internal static void SetStop() { stop = true; }

        static OverlayForm Ensure()
        {
            if (!Enabled) return null;
            lock (gate)
            {
                if (thread == null)
                {
                    System.Threading.ManualResetEvent ready = new System.Threading.ManualResetEvent(false);
                    thread = new System.Threading.Thread(delegate ()
                    {
                        try
                        {
                            form = new OverlayForm();
                            IntPtr h = form.Handle;     // 窓を作る（まだ出さない）
                            ready.Set();
                            System.Windows.Forms.Application.Run();
                        }
                        catch (Exception) { form = null; ready.Set(); }
                    });
                    thread.IsBackground = true;
                    thread.SetApartmentState(System.Threading.ApartmentState.STA);
                    thread.Start();
                    ready.WaitOne(3000);
                }
                return form;
            }
        }

        // 札の文を出す（x, y が -99999 なら印は動かさない）
        public static void Say(string text, int x, int y, bool tap)
        {
            OverlayForm f = Ensure();
            if (f == null) return;
            try { f.BeginInvoke(new Action(delegate () { f.ShowAt(text, x, y, tap); })); } catch (Exception) { }
        }

        // 写真を撮る間だけ隠す（戻るまで待つ）
        public static void Hidden(bool on)
        {
            OverlayForm f;
            lock (gate) { f = form; }
            if (f == null) return;
            try { f.Invoke(new Action(delegate () { f.SetHidden(on); })); } catch (Exception) { }
            if (on) System.Threading.Thread.Sleep(60);    // 画面から消えるのを待つ
        }
    }

    class OverlayForm : System.Windows.Forms.Form
    {
        delegate IntPtr LowLevelKeyboardProc(int nCode, IntPtr wParam, IntPtr lParam);
        [DllImport("user32.dll", SetLastError = true)] static extern IntPtr SetWindowsHookEx(int idHook, LowLevelKeyboardProc fn, IntPtr hMod, uint threadId);
        [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hhk);
        [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hhk, int nCode, IntPtr wParam, IntPtr lParam);
        [DllImport("kernel32.dll")] static extern IntPtr GetModuleHandle(string name);
        [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vKey);

        string text = "";
        System.Drawing.Point cur = new System.Drawing.Point(-1000, -1000);
        int tap = -1;
        DateTime last = DateTime.UtcNow;
        bool hiddenForShot;
        double phase;
        IntPtr hook = IntPtr.Zero;
        LowLevelKeyboardProc proc;                     // GC に回収されないよう持っておく
        readonly System.Windows.Forms.Timer timer = new System.Windows.Forms.Timer();
        static readonly System.Drawing.Color Red = System.Drawing.Color.FromArgb(208, 52, 44);
        static readonly System.Drawing.Color Blue = System.Drawing.Color.FromArgb(47, 111, 224);

        public OverlayForm()
        {
            FormBorderStyle = System.Windows.Forms.FormBorderStyle.None;
            ShowInTaskbar = false;
            TopMost = true;
            StartPosition = System.Windows.Forms.FormStartPosition.Manual;
            Bounds = System.Windows.Forms.SystemInformation.VirtualScreen;
            BackColor = System.Drawing.Color.Magenta;
            TransparencyKey = System.Drawing.Color.Magenta;
            DoubleBuffered = true;
            timer.Interval = 80;
            timer.Tick += Tick;
            proc = KeyHook;
        }

        protected override System.Windows.Forms.CreateParams CreateParams
        {
            get
            {
                System.Windows.Forms.CreateParams cp = base.CreateParams;
                // LAYERED | TRANSPARENT（クリックを通す）| TOOLWINDOW（Alt+Tab に出さない）| NOACTIVATE | TOPMOST
                cp.ExStyle |= 0x80000 | 0x20 | 0x80 | 0x8000000 | 0x8;
                return cp;
            }
        }
        protected override bool ShowWithoutActivation { get { return true; } }

        public void ShowAt(string t, int x, int y, bool doTap)
        {
            if (t != null) text = t;
            if (x > -99999) { cur = new System.Drawing.Point(x - Bounds.Left, y - Bounds.Top); if (doTap) tap = 0; }
            last = DateTime.UtcNow;
            if (!Visible && !hiddenForShot) { Show(); Hook(true); }
            timer.Start();
            Invalidate();
        }

        public void SetHidden(bool on)
        {
            if (on) { if (Visible) { Hide(); hiddenForShot = true; } }
            else if (hiddenForShot) { hiddenForShot = false; Show(); }
        }

        void Tick(object s, EventArgs e)
        {
            if ((DateTime.UtcNow - last).TotalSeconds > 6)
            {
                timer.Stop(); Hide(); Hook(false); text = ""; tap = -1;
                return;
            }
            phase += timer.Interval / 2000.0;          // 2 秒で 1 周
            Opacity = 0.72 + 0.28 * (0.5 + 0.5 * Math.Sin(phase * 2 * Math.PI));
            if (tap >= 0) { tap++; if (tap > 6) tap = -1; Invalidate(); }
        }

        void Hook(bool on)
        {
            if (on && hook == IntPtr.Zero) hook = SetWindowsHookEx(13 /* WH_KEYBOARD_LL */, proc, GetModuleHandle(null), 0);
            else if (!on && hook != IntPtr.Zero) { UnhookWindowsHookEx(hook); hook = IntPtr.Zero; }
        }

        IntPtr KeyHook(int nCode, IntPtr wParam, IntPtr lParam)
        {
            // ★止めるキーは Ctrl+Alt+Q（2026-11-22 オーナー「ESC はよく使うキーだし」→ 旧 Esc をやめた）。
            //   KBDLLHOOKSTRUCT: vkCode（+0）・flags（+8）。LLKHF_INJECTED（0x10）＝SendInput で送られたキー＝L.A.P.S 自身の key
            //   操作は数えない（旧 Esc では、モデルが key=esc を送ると自分を止め、その Esc も画面に渡らなかった）。
            if (nCode >= 0 && Visible && (wParam == (IntPtr)0x100 || wParam == (IntPtr)0x104)
                && Marshal.ReadInt32(lParam) == 0x51 /* Q */ && (Marshal.ReadInt32(lParam, 8) & 0x10) == 0
                && GetAsyncKeyState(0x11 /* Ctrl */) < 0 && GetAsyncKeyState(0x12 /* Alt */) < 0)
            {
                Overlay.SetStop();
                text = "止めています…";
                Invalidate();
                return (IntPtr)1;                       // 止めるキーは画面に渡さない（ページの中の指示で消されないように）
            }
            return CallNextHookEx(hook, nCode, wParam, lParam);
        }

        protected override void OnPaint(System.Windows.Forms.PaintEventArgs e)
        {
            System.Drawing.Graphics g = e.Graphics;
            g.SmoothingMode = System.Drawing.Drawing2D.SmoothingMode.AntiAlias;
            // 縁の光（モニタごと）
            foreach (System.Windows.Forms.Screen sc in System.Windows.Forms.Screen.AllScreens)
            {
                System.Drawing.Rectangle b = sc.Bounds;
                b.Offset(-Bounds.Left, -Bounds.Top);
                using (System.Drawing.Drawing2D.LinearGradientBrush br =
                    new System.Drawing.Drawing2D.LinearGradientBrush(b, Red, Blue, 45f))
                using (System.Drawing.Pen pen = new System.Drawing.Pen(br, 5f))
                    g.DrawRectangle(pen, b.X + 2, b.Y + 2, b.Width - 5, b.Height - 5);
            }
            // 押す場所の印（輪）と波紋
            if (cur.X > -500)
            {
                using (System.Drawing.Pen ring = new System.Drawing.Pen(Red, 3f))
                    g.DrawEllipse(ring, cur.X - 11, cur.Y - 11, 22, 22);
                using (System.Drawing.SolidBrush dot = new System.Drawing.SolidBrush(Blue))
                    g.FillEllipse(dot, cur.X - 4, cur.Y - 4, 8, 8);
                if (tap >= 0)
                {
                    int r = 14 + tap * 6;
                    using (System.Drawing.Pen wave = new System.Drawing.Pen(Red, 2f))
                        g.DrawEllipse(wave, cur.X - r, cur.Y - r, r * 2, r * 2);
                }
            }
            // 右下の札（主のモニタ）
            System.Drawing.Rectangle p = System.Windows.Forms.Screen.PrimaryScreen.WorkingArea;
            p.Offset(-Bounds.Left, -Bounds.Top);
            string line = "L.A.P.S が画面を操作中" + (text.Length > 0 ? "  —  " + text : "") + "      Ctrl+Alt+Q で止める";
            using (System.Drawing.Font f = new System.Drawing.Font("Yu Gothic UI", 10f, System.Drawing.FontStyle.Bold))
            {
                System.Drawing.SizeF sz = g.MeasureString(line, f);
                int w = (int)Math.Min(sz.Width + 28, p.Width - 40), h = (int)sz.Height + 14;
                System.Drawing.Rectangle box = new System.Drawing.Rectangle(p.Right - w - 16, p.Bottom - h - 16, w, h);
                using (System.Drawing.SolidBrush bg = new System.Drawing.SolidBrush(System.Drawing.Color.FromArgb(24, 24, 27)))
                    g.FillRectangle(bg, box);
                using (System.Drawing.SolidBrush mark = new System.Drawing.SolidBrush(Red))
                    g.FillRectangle(mark, box.X, box.Y, 4, box.Height);
                using (System.Drawing.SolidBrush fg = new System.Drawing.SolidBrush(System.Drawing.Color.FromArgb(244, 244, 245)))
                    g.DrawString(line, f, fg, new System.Drawing.RectangleF(box.X + 14, box.Y + 7, box.Width - 18, box.Height - 8));
            }
        }

        protected override void OnFormClosed(System.Windows.Forms.FormClosedEventArgs e) { Hook(false); base.OnFormClosed(e); }
    }
#endif

    static class Native
    {
        [DllImport("user32.dll", SetLastError = true)] public static extern uint SendInput(uint n, INPUT[] inputs, int size);
        [DllImport("user32.dll")] public static extern bool SetCursorPos(int x, int y);
        [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
        [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
        [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
        [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
        [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
        [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
        [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
        [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool attach);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern short VkKeyScanW(char c);
        [DllImport("user32.dll")] public static extern uint MapVirtualKeyW(uint code, uint type);
        [DllImport("user32.dll")] public static extern IntPtr GetAncestor(IntPtr h, uint flags);
        [DllImport("user32.dll")] public static extern bool SetProcessDPIAware();
        [DllImport("user32.dll")] public static extern bool IsWindow(IntPtr h);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextW(IntPtr h, StringBuilder text, int max);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetClassNameW(IntPtr h, StringBuilder text, int max);
        [DllImport("user32.dll")] public static extern bool GetWindowRect(IntPtr h, out RECT rect);
        [DllImport("user32.dll")] public static extern bool PrintWindow(IntPtr h, IntPtr hdc, uint flags);
        [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
        [DllImport("user32.dll")] public static extern bool EnumWindows(EnumWindowsProc cb, IntPtr lParam);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowTextLengthW(IntPtr h);
        [DllImport("user32.dll")] public static extern int GetWindowLongW(IntPtr h, int index);
        [DllImport("user32.dll")] public static extern IntPtr GetLastActivePopup(IntPtr h);
        [DllImport("dwmapi.dll")] public static extern int DwmGetWindowAttribute(IntPtr h, int attr, out int value, int size);
        [DllImport("user32.dll")] public static extern IntPtr WindowFromPoint(POINT p);
        [DllImport("user32.dll")] public static extern IntPtr MonitorFromPoint(POINT p, uint flags);
        [DllImport("user32.dll")] public static extern IntPtr MonitorFromWindow(IntPtr h, uint flags);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern bool GetMonitorInfoW(IntPtr hMonitor, ref MONITORINFO info);
        [DllImport("user32.dll")] public static extern bool EnumDisplayMonitors(IntPtr hdc, IntPtr clip, MonitorEnumProc cb, IntPtr data);
        [DllImport("user32.dll")] public static extern bool SetWindowPos(IntPtr h, IntPtr after, int x, int y, int cx, int cy, uint flags);
        [DllImport("shcore.dll")] public static extern int GetDpiForMonitor(IntPtr hMonitor, int type, out uint dpiX, out uint dpiY);
        [DllImport("user32.dll", EntryPoint = "SetProcessDpiAwarenessContext")]
        public static extern bool SetProcessDpiAwarenessContext(IntPtr value);
    }

    class UserError : Exception
    {
        public UserError(string message) : base(message) { }
    }

    static class Program
    {
        const uint INPUT_MOUSE = 0, INPUT_KEYBOARD = 1;
        const uint KEYEVENTF_EXTENDEDKEY = 0x1, KEYEVENTF_KEYUP = 0x2, KEYEVENTF_UNICODE = 0x4;
        const uint MOUSEEVENTF_WHEEL = 0x0800;
        const int MAX_LINES = 400;
        const int NEW_WINDOW_LINES = 150;     // the snapshot attached when an action opened a window
        const int VALUE_CHARS = 500;
        const int READ_CHARS = 30000;
        const int MAX_VISITED = 6000;
        const int SNAPSHOT_BUDGET_MS = 15000;

        static readonly Dictionary<int, AutomationElement> ids = new Dictionary<int, AutomationElement>();
        static readonly Dictionary<string, int> idByRuntime = new Dictionary<string, int>();
        // Window handles as last shown to the model (w1, w2, ...). Win32 facts only: reading a title
        // through UI Automation waits up to ~20 s when that application is not answering.
        static readonly List<IntPtr> windows = new List<IntPtr>();
        // ids / idByRuntime are also touched by the snapshot thread.
        static readonly object idLock = new object();
        static int nextId = 1;
        // The window Blackwall last worked in. Keys and text without a target go here, not to
        // whatever window the user has in front.
        static IntPtr lastTop = IntPtr.Zero;

        static int Main(string[] args)
        {
            SetDpiAwareness();
            JavaScriptSerializer json = new JavaScriptSerializer();
            json.MaxJsonLength = 16 * 1024 * 1024;
            Reply(true, "bw-desktop ready");
            string line;
            while ((line = Console.In.ReadLine()) != null)
            {
                if (line.Trim().Length == 0) continue;
                bool ok = true;
                string text;
                try
                {
                    Dictionary<string, object> req = json.DeserializeObject(line) as Dictionary<string, object>;
                    if (req == null) throw new UserError("Error: the request is not a JSON object.");
                    text = Handle(req);
                }
                catch (UserError e) { ok = false; text = e.Message; }
                catch (ElementNotAvailableException) { ok = false; text = "Error: that control no longer exists (the window changed). Take a new snapshot."; }
                catch (Exception e) { ok = false; text = "Error: " + e.GetType().Name + ": " + e.Message; }
                Reply(ok, text);
            }
            return 0;
        }

        static void SetDpiAwareness()
        {
            // Per-monitor v2 so that UI Automation rectangles and SetCursorPos use the same pixels.
            try { if (Native.SetProcessDpiAwarenessContext(new IntPtr(-4))) return; }
            catch (EntryPointNotFoundException) { }
            catch (Exception) { }
            try { Native.SetProcessDPIAware(); } catch (Exception) { }
        }

        static void Reply(bool ok, string text)
        {
            Console.Out.Write("{\"ok\":" + (ok ? "true" : "false") + ",\"text\":\"" + Esc(text) + "\"}\n");
            Console.Out.Flush();
        }

        static string Esc(string s)
        {
            StringBuilder sb = new StringBuilder();
            foreach (char c in s ?? "")
            {
                if (c == '"') sb.Append("\\\"");
                else if (c == '\\') sb.Append("\\\\");
                else if (c == '\n') sb.Append("\\n");
                else if (c == '\r') sb.Append("\\r");
                else if (c == '\t') sb.Append("\\t");
                else if (c < 0x20 || c > 0x7e) sb.Append("\\u").Append(((int)c).ToString("x4"));
                else sb.Append(c);
            }
            return sb.ToString();
        }

        // ---- request fields -------------------------------------------------------

        static string Str(Dictionary<string, object> r, string k)
        {
            object v;
            if (!r.TryGetValue(k, out v) || v == null) return "";
            return Convert.ToString(v, System.Globalization.CultureInfo.InvariantCulture);
        }

        static bool Has(Dictionary<string, object> r, string k)
        {
            object v;
            return r.TryGetValue(k, out v) && v != null && Convert.ToString(v).Length > 0;
        }

        static int Int(Dictionary<string, object> r, string k, int dflt)
        {
            object v;
            if (!r.TryGetValue(k, out v) || v == null) return dflt;
            try { return Convert.ToInt32(Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture)); }
            catch (Exception) { throw new UserError("Error: '" + k + "' must be a number."); }
        }

        static double Num(Dictionary<string, object> r, string k, double dflt)
        {
            object v;
            if (!r.TryGetValue(k, out v) || v == null) return dflt;
            try { return Convert.ToDouble(v, System.Globalization.CultureInfo.InvariantCulture); }
            catch (Exception) { throw new UserError("Error: '" + k + "' must be a number."); }
        }

        static bool Bool(Dictionary<string, object> r, string k)
        {
            object v;
            if (!r.TryGetValue(k, out v) || v == null) return false;
            if (v is bool) return (bool)v;
            string s = Convert.ToString(v).Trim().ToLowerInvariant();
            return s == "true" || s == "1" || s == "yes";
        }

        // ---- dispatch -------------------------------------------------------------

        static string Handle(Dictionary<string, object> r)
        {
            string action = Str(r, "action").Trim().ToLowerInvariant();
            LoadGuard(r);
#if BW_OVERLAY
            Overlay.Enabled = !Has(r, "indicator") || Bool(r, "indicator");
            bool acting = action == "click" || action == "type" || action == "key" || action == "set_value" || action == "scroll"
                || action == "move" || action == "drag" || action == "launch" || action == "focus";
            if (acting)
            {
                if (Overlay.TakeStop())
                    throw new UserError("Error: the user pressed the stop key (Ctrl+Alt+Q) to stop L.A.P.S, so this action was not done. Do not keep operating "
                        + "the screen; tell the user where you stopped and ask what to do next.");
                Overlay.Say(Has(r, "label") ? Str(r, "label") : action, -99999, -99999, false);
            }
            if (action == "look" || action == "ocr")
            {
                Overlay.Hidden(true);              // 写真に表示を入れない
                try { return action == "look" ? Look(r) : Ocr(r); }
                finally { Overlay.Hidden(false); }
            }
#endif
            switch (action)
            {
                case "windows": return ListWindows();
                case "snapshot": return Snapshot(r);
                case "web_snapshot": return WebSnapshot(r);
                case "read": return ReadText(r);
                case "ocr": return Ocr(r);
                case "look": return Look(r);
                case "launch": return Launch(r);
                case "click": return Watch(r, Click);
                case "type": return Watch(r, TypeText);
                case "key": return Watch(r, Keys);
                case "set_value": return Watch(r, SetValue);
                case "scroll": return Watch(r, Scroll);
                case "move": return MoveMouse(r);
                case "drag": return Watch(r, Drag);
                case "cursor": return CursorAt();
                case "focus": return Focus(r);
                case "ping": return "pong";
            }
            throw new UserError("Error: unknown action '" + action + "'. Use one of: windows, snapshot, read, ocr, look, launch, focus, click, type, set_value, key, scroll, move, drag, cursor.");
        }

        // Runs an action and reports the windows it opened or closed. A window that opened and is
        // now in front (a dialog, usually) comes with its snapshot, which saves a step.
        static string Watch(Dictionary<string, object> r, Func<Dictionary<string, object>, string> act)
        {
            Dictionary<IntPtr, string> before = new Dictionary<IntPtr, string>();
            foreach (IntPtr h in TopWindows()) before[h] = Title(h);
            string text = act(r);
            List<IntPtr> after = TopWindows();
            StringBuilder sb = new StringBuilder(text);
            IntPtr fg = Native.GetForegroundWindow();
            IntPtr opened = IntPtr.Zero;
            foreach (IntPtr h in after)
            {
                if (before.ContainsKey(h)) continue;
                sb.Append("\nOpened window \"").Append(Clean(Title(h), 100)).Append("\" - ").Append(ProcName(PidOf(h)));
                if (h == fg || opened == IntPtr.Zero) opened = h;
            }
            HashSet<IntPtr> now = new HashSet<IntPtr>(after);
            foreach (KeyValuePair<IntPtr, string> kv in before)
                if (!now.Contains(kv.Key)) sb.Append("\nClosed window \"").Append(Clean(kv.Value, 100)).Append('"');
            if (opened != IntPtr.Zero)
            {
                Thread.Sleep(300);
                sb.Append("\n\n").Append(SnapshotText(null, opened, WindowHead(opened), NEW_WINDOW_LINES, false));
            }
            return sb.ToString();
        }

        // ---- cached properties ----------------------------------------------------

        static CacheRequest NewCache()
        {
            CacheRequest c = new CacheRequest();
            c.TreeScope = TreeScope.Element;
            c.TreeFilter = Automation.ControlViewCondition;
            c.Add(AutomationElement.NameProperty);
            c.Add(AutomationElement.ControlTypeProperty);
            c.Add(AutomationElement.AutomationIdProperty);
            c.Add(AutomationElement.IsEnabledProperty);
            c.Add(AutomationElement.IsOffscreenProperty);
            c.Add(AutomationElement.HasKeyboardFocusProperty);
            c.Add(AutomationElement.RuntimeIdProperty);
            c.Add(AutomationElement.ProcessIdProperty);
            c.Add(AutomationElement.NativeWindowHandleProperty);
            c.Add(AutomationElement.ClassNameProperty);
            c.Add(AutomationElement.IsPasswordProperty);
            c.Add(ValuePattern.ValueProperty);
            c.Add(TogglePattern.ToggleStateProperty);
            c.Add(SelectionItemPattern.IsSelectedProperty);
            c.Add(ExpandCollapsePattern.ExpandCollapseStateProperty);
            c.Add(AutomationElement.HelpTextProperty);
            c.Add(AutomationElement.AcceleratorKeyProperty);
            c.Add(AutomationElement.AccessKeyProperty);
            c.Add(AutomationElement.ItemStatusProperty);
            c.Add(ValuePattern.IsReadOnlyProperty);
            c.Add(RangeValuePattern.ValueProperty);
            c.Add(RangeValuePattern.MinimumProperty);
            c.Add(RangeValuePattern.MaximumProperty);
            c.Add(GridPattern.RowCountProperty);
            c.Add(GridPattern.ColumnCountProperty);
            c.Add(WindowPattern.IsModalProperty);
            c.Add(AutomationElement.IsTextPatternAvailableProperty);
            return c;
        }

        static object CP(AutomationElement e, AutomationProperty p)
        {
            try
            {
                object v = e.GetCachedPropertyValue(p, true);
                if (v == AutomationElement.NotSupported) return null;
                return v;
            }
            catch (Exception) { return null; }
        }

        static string CStr(AutomationElement e, AutomationProperty p)
        {
            object v = CP(e, p);
            return v == null ? "" : Convert.ToString(v);
        }

        static bool CBool(AutomationElement e, AutomationProperty p)
        {
            object v = CP(e, p);
            return v is bool && (bool)v;
        }

        static int CInt(AutomationElement e, AutomationProperty p)
        {
            object v = CP(e, p);
            try { return v == null ? 0 : Convert.ToInt32(v); } catch (Exception) { return 0; }
        }

        static string TypeName(ControlType ct)
        {
            if (ct == null) return "Unknown";
            string n = ct.ProgrammaticName ?? "";
            int dot = n.LastIndexOf('.');
            return dot >= 0 ? n.Substring(dot + 1) : n;
        }

        static string Clean(string s, int max)
        {
            if (string.IsNullOrEmpty(s)) return "";
            StringBuilder sb = new StringBuilder();
            bool space = false;
            foreach (char c in s)
            {
                if (char.IsWhiteSpace(c) || char.IsControl(c)) { space = true; continue; }
                if (space && sb.Length > 0) sb.Append(' ');
                space = false;
                sb.Append(c);
            }
            string t = sb.ToString();
            if (t.Length > max) t = t.Substring(0, max) + "...";
            return t.Replace("\"", "'");
        }

        static string ProcName(int pid)
        {
            try { return Process.GetProcessById(pid).ProcessName; }
            catch (Exception) { return "pid " + pid; }
        }

        // ---- ids -----------------------------------------------------------------

        static string RuntimeKey(int[] rt)
        {
            if (rt == null || rt.Length == 0) return null;
            StringBuilder sb = new StringBuilder();
            foreach (int x in rt) sb.Append(x).Append('.');
            return sb.ToString();
        }

        static int IdFor(AutomationElement e)
        {
            string key = RuntimeKey(CP(e, AutomationElement.RuntimeIdProperty) as int[]);
            lock (idLock)
            {
                int id;
                if (key != null && idByRuntime.TryGetValue(key, out id))
                {
                    ids[id] = e;
                    return id;
                }
                if (ids.Count > 20000) { ids.Clear(); idByRuntime.Clear(); }
                id = nextId++;
                ids[id] = e;
                if (key != null) idByRuntime[key] = id;
                return id;
            }
        }

        static AutomationElement ById(int id)
        {
            AutomationElement e;
            lock (idLock)
            {
                if (ids.TryGetValue(id, out e)) return e;
            }
            throw new UserError("Error: there is no control [" + id + "]. Ids come from snapshot; take a snapshot first.");
        }

        static string KnownId(string runtimeKey)
        {
            int known;
            lock (idLock)
            {
                if (runtimeKey != null && idByRuntime.TryGetValue(runtimeKey, out known)) return "[" + known + "] ";
            }
            return "";
        }

        static string Label(AutomationElement e)
        {
            try
            {
                string t = TypeName(e.Current.ControlType);
                string n = Clean(e.Current.Name, 60);
                return KnownId(RuntimeKey(e.GetRuntimeId())) + t + (n.Length > 0 ? " \"" + n + "\"" : "");
            }
            catch (Exception) { return "(a control that no longer exists)"; }
        }

        // ---- windows ---------------------------------------------------------------

        static string Title(IntPtr h)
        {
            StringBuilder sb = new StringBuilder(512);
            Native.GetWindowTextW(h, sb, sb.Capacity);
            return sb.ToString();
        }

        static string ClassOf(IntPtr h)
        {
            StringBuilder sb = new StringBuilder(256);
            Native.GetClassNameW(h, sb, sb.Capacity);
            return sb.ToString();
        }

        static int PidOf(IntPtr h)
        {
            uint pid;
            Native.GetWindowThreadProcessId(h, out pid);
            return (int)pid;
        }

        // Top-level windows in front-to-back order, from EnumWindows: the UI Automation tree puts
        // owned windows (dialogs such as a folder picker) under their owner, so a dialog in front
        // could not be found by its title.
        static List<IntPtr> TopWindows()
        {
            List<IntPtr> handles = new List<IntPtr>();
            EnumWindowsProc cb = delegate(IntPtr h, IntPtr l)
            {
                if (!Native.IsWindowVisible(h)) return true;
                if (Native.GetWindowTextLengthW(h) == 0) return true;
                if ((Native.GetWindowLongW(h, -20) & 0x80) != 0) return true;   // WS_EX_TOOLWINDOW
                int cloaked;
                try { if (Native.DwmGetWindowAttribute(h, 14, out cloaked, 4) == 0 && cloaked != 0) return true; }
                catch (Exception) { }
                string cls = ClassOf(h);
                if (cls == "Progman" || cls == "WorkerW") return true;
                handles.Add(h);
                return true;
            };
            Native.EnumWindows(cb, IntPtr.Zero);
            GC.KeepAlive(cb);
            return handles;
        }

        static string WindowLine(int n, IntPtr h, IntPtr fg, List<ScreenInfo> screens)
        {
            string s = "w" + n + " \"" + Clean(Title(h), 100) + "\" - " + ProcName(PidOf(h));
            if (screens != null && !Native.IsIconic(h))
            {
                int on = ScreenOf(h, screens);
                if (on == 0) s += " (on no screen: focus moves it back)";
                else if (screens.Count > 1) s += " on s" + on;
            }
            if (h == fg) s += " (foreground)";
            if (Native.IsIconic(h)) s += " (minimized)";
            if (Guarded(h)) s += " (Blackwall itself: read only)";
            return s;
        }

        static string ListWindows()
        {
            windows.Clear();
            windows.AddRange(TopWindows());
            IntPtr fg = Native.GetForegroundWindow();
            List<ScreenInfo> screens = Screens();
            StringBuilder sb = new StringBuilder();
            sb.Append(ScreensLine()).Append('\n');
            sb.Append(windows.Count).Append(" windows:\n");
            for (int i = 0; i < windows.Count; i++) sb.Append(WindowLine(i + 1, windows[i], fg, screens)).Append('\n');
            return sb.ToString().TrimEnd();
        }

        static string WindowList()
        {
            IntPtr fg = Native.GetForegroundWindow();
            List<ScreenInfo> screens = Screens();
            StringBuilder sb = new StringBuilder();
            for (int i = 0; i < windows.Count; i++) sb.Append("\n  ").Append(WindowLine(i + 1, windows[i], fg, screens));
            return windows.Count == 0 ? " (none)" : sb.ToString();
        }

        static int WindowNumber(IntPtr h)
        {
            for (int i = 0; i < windows.Count; i++) if (windows[i] == h) return i + 1;
            return 0;
        }

        static IntPtr FindWindow(string spec)
        {
            spec = (spec ?? "").Trim();
            if (spec.Length == 0) return Native.GetForegroundWindow();
            string num = spec.StartsWith("w", StringComparison.OrdinalIgnoreCase) ? spec.Substring(1) : spec;
            int n;
            if (int.TryParse(num, out n))
            {
                if (windows.Count == 0) windows.AddRange(TopWindows());
                if (n >= 1 && n <= windows.Count && Native.IsWindow(windows[n - 1])) return windows[n - 1];
                return IntPtr.Zero;
            }
            // A title search reads a fresh list of its own: the w-numbers the model was shown
            // must keep meaning the same windows until it is shown a new list.
            string low = spec.ToLowerInvariant();
            string proc = low.EndsWith(".exe") ? low.Substring(0, low.Length - 4) : low;
            IntPtr fg = Native.GetForegroundWindow();
            IntPtr best = IntPtr.Zero, self = IntPtr.Zero;
            foreach (IntPtr h in TopWindows())
            {
                if (Title(h).ToLowerInvariant().Contains(low) || ProcName(PidOf(h)).ToLowerInvariant() == proc)
                {
                    // A title search must not pick Blackwall's own window while another window matches
                    // (the app's title contains "Blackwall"); alone, it is returned for reading only.
                    if (Guarded(h)) { if (self == IntPtr.Zero) self = h; continue; }
                    if (h == fg) return h;
                    if (best == IntPtr.Zero) best = h;
                }
            }
            return best != IntPtr.Zero ? best : self;
        }

        static IntPtr ResolveWindow(string spec, double waitSeconds)
        {
            Stopwatch sw = Stopwatch.StartNew();
            while (true)
            {
                IntPtr w = FindWindow(spec);
                if (w != IntPtr.Zero) return w;
                if (sw.Elapsed.TotalSeconds >= waitSeconds) break;
                Thread.Sleep(300);
            }
            if ((spec ?? "").Trim().Length == 0) throw new UserError("Error: there is no foreground window.");
            windows.Clear();
            windows.AddRange(TopWindows());
            throw new UserError("Error: no window matches '" + spec + "'. Open windows:" + WindowList());
        }

        static IntPtr TopHandle(AutomationElement e)
        {
            TreeWalker walker = TreeWalker.ControlViewWalker;
            AutomationElement cur = e;
            for (int i = 0; i < 60 && cur != null; i++)
            {
                int h = 0;
                try { h = cur.Current.NativeWindowHandle; } catch (Exception) { }
                if (h != 0) return Native.GetAncestor(new IntPtr(h), 2);
                try { cur = walker.GetParent(cur); } catch (Exception) { return IntPtr.Zero; }
                if (cur != null && Automation.Compare(cur, AutomationElement.RootElement)) return IntPtr.Zero;
            }
            return IntPtr.Zero;
        }

        static bool Activate(IntPtr h)
        {
            if (h == IntPtr.Zero) return false;
            if (Native.IsIconic(h)) { Native.ShowWindow(h, 9); Thread.Sleep(200); }
            if (Native.GetForegroundWindow() == h) return true;
            IntPtr fg = Native.GetForegroundWindow();
            uint pid;
            uint fgThread = fg == IntPtr.Zero ? 0 : Native.GetWindowThreadProcessId(fg, out pid);
            uint me = Native.GetCurrentThreadId();
            bool attached = fgThread != 0 && fgThread != me && Native.AttachThreadInput(me, fgThread, true);
            Native.BringWindowToTop(h);
            Native.SetForegroundWindow(h);
            if (attached) Native.AttachThreadInput(me, fgThread, false);
            for (int i = 0; i < 10 && Native.GetForegroundWindow() != h; i++) Thread.Sleep(30);
            if (Native.GetForegroundWindow() != h)
            {
                try { AutomationElement.FromHandle(h).SetFocus(); } catch (Exception) { }
                Thread.Sleep(150);
            }
            return Native.GetForegroundWindow() == h;
        }

        static void ActivateFor(Dictionary<string, object> r, AutomationElement el)
        {
            string what = Str(r, "action");
            if (el != null)
            {
                IntPtr t = TopHandle(el);
                RefuseSelf(t, what);
                if (t != IntPtr.Zero) lastTop = t;
                Activate(t);
                return;
            }
            if (Has(r, "window"))
            {
                IntPtr h = ResolveWindow(Str(r, "window"), Num(r, "wait", 0));
                RefuseSelf(h, what);
                lastTop = h;
                if (!Activate(h)) throw new UserError("Error: could not bring the window to the front (Windows refused). Click inside it instead.");
            }
        }

        // Keys, text and clicks by coordinates go to whatever window is in front. When the
        // model names no target and the front window is Blackwall itself (the app the user
        // is talking to it in), the input would land in the user's own chat box.
        static void GuardForeground(Dictionary<string, object> r, string what)
        {
            if (Has(r, "id") || Has(r, "window")) return;
            object v;
            if (!r.TryGetValue("guard_pids", out v) || v == null) return;
            object[] arr = v as object[];
            if (arr == null) return;
            IntPtr fg = Native.GetForegroundWindow();
            if (fg == IntPtr.Zero) return;
            uint pid;
            Native.GetWindowThreadProcessId(fg, out pid);
            foreach (object o in arr)
            {
                int g;
                try { g = Convert.ToInt32(o); } catch (Exception) { continue; }
                if (g == (int)pid)
                    throw new UserError("Error: the window in front is Blackwall itself, so the " + what
                        + " would go into the user's chat. Pass window (or id) to choose the target.");
            }
        }

        // ---- never act on Blackwall itself (2026-10-19) ------------------------------------
        // The owner saw the agent close its own app. guard_pids (sent with every request) are the
        // agent's process, the app that runs it, and the app's web view. Reading Blackwall's window is
        // allowed; clicking, typing, keys, scrolling and focusing it are refused on every path.
        static readonly HashSet<int> guardPids = new HashSet<int>();

        static void LoadGuard(Dictionary<string, object> r)
        {
            guardPids.Clear();
            object v;
            if (!r.TryGetValue("guard_pids", out v) || v == null) return;
            object[] arr = v as object[];
            if (arr == null) return;
            foreach (object o in arr)
            {
                try { int g = Convert.ToInt32(o); if (g > 4) guardPids.Add(g); } catch (Exception) { }
            }
        }

        static bool Guarded(IntPtr h)
        {
            if (h == IntPtr.Zero || guardPids.Count == 0) return false;
            IntPtr root = Native.GetAncestor(h, 2);   // GA_ROOT: the web view's windows belong to the app's window
            if (root == IntPtr.Zero) root = h;
            uint pid;
            Native.GetWindowThreadProcessId(root, out pid);
            if (guardPids.Contains((int)pid)) return true;
            Native.GetWindowThreadProcessId(h, out pid);
            return guardPids.Contains((int)pid);
        }

        const string SELF_WHY = " is Blackwall's own window (the app this agent runs in); acting on it can stop this session.";

        static void RefuseSelf(IntPtr h, string what)
        {
            if (Guarded(h))
                throw new UserError("Error: not done: the " + what + " would go to \"" + Clean(Title(Native.GetAncestor(h, 2)), 60)
                    + "\", which" + SELF_WHY + " Name the window you mean (windows lists them with their screens).");
        }

        // Keys and text go to the window in front. After every attempt to target a window, refuse
        // if that is still Blackwall (Windows sometimes refuses to change the foreground window).
        static void RefuseSelfForeground(string what)
        {
            IntPtr fg = Native.GetForegroundWindow();
            if (Guarded(fg))
                throw new UserError("Error: not done: Blackwall's own window is in front, so the " + what + " would go into it"
                    + " (Windows did not bring the target window to the front). Click inside the target window first, or use id with set_value.");
        }

        // A screen point that will be clicked or scrolled: it must be on a screen and not on Blackwall.
        static void CheckPoint(int x, int y, string what)
        {
            POINT p; p.X = x; p.Y = y;
            if (Native.MonitorFromPoint(p, 0) == IntPtr.Zero)   // MONITOR_DEFAULTTONULL
                throw new UserError("Error: " + x + "," + y + " is not on any screen. " + ScreensLine()
                    + " (x and y are desktop pixels; a screen left of or above the main one has negative ones.)");
            IntPtr h = Native.WindowFromPoint(p);
            if (Guarded(h))
                throw new UserError("Error: not done: the " + what + " at " + x + "," + y + " would land on \""
                    + Clean(Title(Native.GetAncestor(h, 2)), 60) + "\", which" + SELF_WHY
                    + " Bring the target window to the front first (focus), or move it away from Blackwall.");
        }

        // ---- screens (monitors) --------------------------------------------------------
        class ScreenInfo { public IntPtr Handle; public RECT Bounds; public RECT Work; public bool Primary; public int Scale; }

        static List<ScreenInfo> Screens()
        {
            List<ScreenInfo> list = new List<ScreenInfo>();
            MonitorEnumProc cb = delegate(IntPtr hm, IntPtr hdc, ref RECT rc, IntPtr d)
            {
                MONITORINFO mi = new MONITORINFO();
                mi.cbSize = Marshal.SizeOf(typeof(MONITORINFO));
                ScreenInfo s = new ScreenInfo();
                s.Handle = hm;
                s.Bounds = rc;
                s.Work = rc;
                if (Native.GetMonitorInfoW(hm, ref mi)) { s.Bounds = mi.rcMonitor; s.Work = mi.rcWork; s.Primary = (mi.dwFlags & 1) != 0; }
                s.Scale = 0;
                try { uint dx, dy; if (Native.GetDpiForMonitor(hm, 0, out dx, out dy) == 0 && dx > 0) s.Scale = (int)Math.Round(dx * 100.0 / 96); }
                catch (Exception) { }
                list.Add(s);
                return true;
            };
            try { Native.EnumDisplayMonitors(IntPtr.Zero, IntPtr.Zero, cb, IntPtr.Zero); } catch (Exception) { }
            GC.KeepAlive(cb);
            // The main screen first, then left to right, top to bottom: s1 is always the main one.
            list.Sort(delegate(ScreenInfo a, ScreenInfo b)
            {
                if (a.Primary != b.Primary) return a.Primary ? -1 : 1;
                if (a.Bounds.Left != b.Bounds.Left) return a.Bounds.Left.CompareTo(b.Bounds.Left);
                return a.Bounds.Top.CompareTo(b.Bounds.Top);
            });
            return list;
        }

        static string ScreenText(int n, ScreenInfo s)
        {
            return "s" + n + (s.Primary ? " (main)" : "") + " " + (s.Bounds.Right - s.Bounds.Left) + "x" + (s.Bounds.Bottom - s.Bounds.Top)
                + " at " + s.Bounds.Left + "," + s.Bounds.Top + (s.Scale > 0 ? " (" + s.Scale + "% scale)" : "");
        }

        static string ScreensLine()
        {
            List<ScreenInfo> all = Screens();
            if (all.Count == 0) return "Screens: unknown.";
            StringBuilder sb = new StringBuilder("Screens: ");
            for (int i = 0; i < all.Count; i++) { if (i > 0) sb.Append(", "); sb.Append(ScreenText(i + 1, all[i])); }
            return sb.Append('.').ToString();
        }

        // The screen number (1-based) a window is mostly on; 0 when it is on none.
        static int ScreenOf(IntPtr h, List<ScreenInfo> all)
        {
            IntPtr m = Native.MonitorFromWindow(h, 0);
            if (m == IntPtr.Zero) return 0;
            for (int i = 0; i < all.Count; i++) if (all[i].Handle == m) return i + 1;
            return 0;
        }

        // No waiting for the user (2026-10-05, owner): on the real machine GetLastInputInfo kept
        // reporting activity while nobody touched anything, so every real click and key waited
        // 15 s and gave up. Blackwall has priority on the shared mouse and keyboard.

        // Keys and text without id or window go to the window Blackwall last worked in. If the
        // user has another application in front, bring Blackwall's window (or its open dialog) back.
        static void TargetForKeys(Dictionary<string, object> r)
        {
            if (Has(r, "id") || Has(r, "window")) return;
            if (lastTop == IntPtr.Zero || !Native.IsWindow(lastTop)) return;
            IntPtr fg = Native.GetForegroundWindow();
            uint pf = 0, pl = 0;
            if (fg != IntPtr.Zero) Native.GetWindowThreadProcessId(fg, out pf);
            Native.GetWindowThreadProcessId(lastTop, out pl);
            if (fg != IntPtr.Zero && pf == pl) return;   // the same application (or its dialog) is in front
            IntPtr pop = Native.GetLastActivePopup(lastTop);
            Activate(pop != IntPtr.Zero ? pop : lastTop);
        }

        // What the model needs to know after acting: where the keyboard goes now.
        static string After()
        {
            Thread.Sleep(50);
            StringBuilder sb = new StringBuilder("\nNow: ");
            IntPtr fg = Native.GetForegroundWindow();
            if (fg == IntPtr.Zero) sb.Append("no foreground window");
            else sb.Append("foreground \"").Append(Clean(Title(fg), 80)).Append("\" (").Append(ProcName(PidOf(fg))).Append(")");
            // The focused control through UI Automation, at most 2 s (an application that is not
            // answering would otherwise hold every reply for ~20 s).
            string desc = null, key = null;
            Thread t = new Thread(delegate()
            {
                try
                {
                    AutomationElement f = AutomationElement.FocusedElement;
                    if (f == null) return;
                    string n = Clean(f.Current.Name, 60);
                    key = RuntimeKey(f.GetRuntimeId());
                    desc = TypeName(f.Current.ControlType) + (n.Length > 0 ? " \"" + n + "\"" : "");
                }
                catch (Exception) { }
            });
            t.IsBackground = true;
            t.Start();
            if (t.Join(2000) && desc != null) sb.Append(", focus ").Append(KnownId(key)).Append(desc);
            return sb.ToString();
        }

        // ---- snapshot ----------------------------------------------------------------

        class Walk
        {
            public StringBuilder Out = new StringBuilder();
            public int Lines, Visited, Offscreen;
            public bool Truncated;
            public string Reason = "";
            public volatile bool Abandoned;
            public Stopwatch Clock = Stopwatch.StartNew();
            public CacheRequest Cache;
            public int MaxLines = MAX_LINES;
            public bool IncludeOffscreen;
            public List<int[]> Boxes;   // look: id, left, top, right, bottom (screen pixels); null otherwise
        }

        static readonly HashSet<string> Boring = new HashSet<string>(new string[] {
            "Pane", "Group", "Custom", "Text", "Image", "Separator", "Thumb", "Window", "TitleBar",
            "ToolBar", "StatusBar", "Header", "Table", "DataGrid", "MenuBar", "Tab", "Tree", "List"
        });

        static string Fmt(double d)
        {
            return d.ToString("0.##", System.Globalization.CultureInfo.InvariantCulture);
        }

        static string Describe(AutomationElement e, out bool boring)
        {
            ControlType ct = CP(e, AutomationElement.ControlTypeProperty) as ControlType;
            string type = TypeName(ct);
            string name = Clean(CStr(e, AutomationElement.NameProperty), 120);
            string aid = Clean(CStr(e, AutomationElement.AutomationIdProperty), 40);
            object val = CP(e, ValuePattern.ValueProperty);
            string value = val == null ? "" : Convert.ToString(val);
            bool password = CBool(e, AutomationElement.IsPasswordProperty);
            boring = name.Length == 0 && value.Length == 0 && Boring.Contains(type)
                && !CBool(e, AutomationElement.HasKeyboardFocusProperty);
            if (boring) return null;
            StringBuilder sb = new StringBuilder();
            sb.Append(type);
            if (name.Length > 0) sb.Append(" \"").Append(name).Append('"');
            else if (aid.Length > 0) sb.Append(" #").Append(aid);
            if (password) { if (value.Length > 0) sb.Append(" value=(hidden)"); }
            else if (value.Length > 0 && value != CStr(e, AutomationElement.NameProperty))
            {
                string v = Clean(value, VALUE_CHARS);
                sb.Append(" value=\"").Append(v).Append('"');
                if (value.Length > VALUE_CHARS) sb.Append(" (" + value.Length + " chars; read gives all)");
            }
            object rv = CP(e, RangeValuePattern.ValueProperty);
            if (rv is double)
            {
                object mn = CP(e, RangeValuePattern.MinimumProperty), mx = CP(e, RangeValuePattern.MaximumProperty);
                sb.Append(" value=").Append(Fmt((double)rv));
                if (mn is double && mx is double) sb.Append(" (").Append(Fmt((double)mn)).Append("..").Append(Fmt((double)mx)).Append(')');
            }
            string tip = Clean(CStr(e, AutomationElement.HelpTextProperty), 100);
            if (tip.Length > 0 && tip != name) sb.Append(" tip=\"").Append(tip).Append('"');
            string status = Clean(CStr(e, AutomationElement.ItemStatusProperty), 80);
            if (status.Length > 0) sb.Append(" status=\"").Append(status).Append('"');
            List<string> flags = new List<string>();
            if (!CBool(e, AutomationElement.IsEnabledProperty)) flags.Add("disabled");
            if ((type == "Edit" || type == "ComboBox" || type == "Document") && CBool(e, ValuePattern.IsReadOnlyProperty)) flags.Add("read-only");
            if (CBool(e, WindowPattern.IsModalProperty)) flags.Add("modal");
            object rows = CP(e, GridPattern.RowCountProperty), cols = CP(e, GridPattern.ColumnCountProperty);
            if (rows is int && cols is int && ((int)rows > 0 || (int)cols > 0)) flags.Add((int)rows + " rows x " + (int)cols + " columns");
            string accel = Clean(CStr(e, AutomationElement.AcceleratorKeyProperty), 30);
            if (accel.Length > 0) flags.Add("shortcut " + accel);
            string access = Clean(CStr(e, AutomationElement.AccessKeyProperty), 30);
            if (access.Length > 0) flags.Add("access key " + access);
            if (CBool(e, AutomationElement.HasKeyboardFocusProperty)) flags.Add("focused");
            object ts = CP(e, TogglePattern.ToggleStateProperty);
            if (ts is ToggleState)
            {
                ToggleState t = (ToggleState)ts;
                flags.Add(t == ToggleState.On ? "checked" : t == ToggleState.Off ? "unchecked" : "mixed");
            }
            if (CBool(e, SelectionItemPattern.IsSelectedProperty)) flags.Add("selected");
            object ec = CP(e, ExpandCollapsePattern.ExpandCollapseStateProperty);
            if (ec is ExpandCollapseState)
            {
                ExpandCollapseState s = (ExpandCollapseState)ec;
                if (s == ExpandCollapseState.Collapsed) flags.Add("collapsed");
                else if (s == ExpandCollapseState.Expanded) flags.Add("expanded");
            }
            if (flags.Count > 0) sb.Append(" (").Append(string.Join(", ", flags.ToArray())).Append(')');
            return sb.ToString();
        }

        static void WalkChildren(AutomationElement parent, int depth, Walk w)
        {
            if (depth > 40) return;
            AutomationElementCollection kids;
            try
            {
                using (w.Cache.Activate())
                {
                    kids = parent.FindAll(TreeScope.Children, Automation.ControlViewCondition);
                }
            }
            catch (Exception) { return; }
            foreach (AutomationElement k in kids)
            {
                if (w.Truncated || w.Abandoned) return;
                w.Visited++;
                if (w.Lines >= w.MaxLines) { w.Truncated = true; w.Reason = "lines"; return; }
                if (w.Visited > MAX_VISITED) { w.Truncated = true; w.Reason = "visited"; return; }
                if (w.Clock.ElapsedMilliseconds > SNAPSHOT_BUDGET_MS) { w.Truncated = true; w.Reason = "time"; return; }
                bool off = CBool(k, AutomationElement.IsOffscreenProperty);
                if (off && !w.IncludeOffscreen) { w.Offscreen++; continue; }
                ControlType ct = CP(k, AutomationElement.ControlTypeProperty) as ControlType;
                if (ct == ControlType.ScrollBar) continue;
                bool boring;
                string desc = Describe(k, out boring);
                if (desc != null && off) desc += " (off-screen)";
                int childDepth = depth;
                if (desc != null)
                {
                    int id = IdFor(k);
                    lock (w)
                    {
                        if (w.Abandoned) return;
                        w.Out.Append(new string(' ', Math.Min(depth, 12) * 2)).Append('[').Append(id).Append("] ").Append(desc).Append('\n');
                        w.Lines++;
                        if (w.Boxes != null)
                        {
                            object br = CP(k, AutomationElement.BoundingRectangleProperty);
                            if (br is Rect)
                            {
                                Rect b = (Rect)br;
                                if (!b.IsEmpty && b.Width > 0 && b.Height > 0)
                                    w.Boxes.Add(new int[] { id, (int)b.Left, (int)b.Top, (int)b.Right, (int)b.Bottom });
                            }
                        }
                    }
                    childDepth = depth + 1;
                }
                WalkChildren(k, childDepth, w);
            }
        }

        // The window the model means when it names none: the one it last worked in, else the one
        // in front.
        static IntPtr DefaultWindow(Dictionary<string, object> r)
        {
            if (Has(r, "window")) { IntPtr h = ResolveWindow(Str(r, "window"), Num(r, "wait", 0)); lastTop = h; return h; }
            if (lastTop != IntPtr.Zero && Native.IsWindow(lastTop))
            {
                IntPtr pop = Native.GetLastActivePopup(lastTop);
                return pop != IntPtr.Zero && Native.IsWindowVisible(pop) ? pop : lastTop;
            }
            IntPtr fg = Native.GetForegroundWindow();
            if (fg == IntPtr.Zero) throw new UserError("Error: there is no foreground window.");
            return fg;
        }

        static string WindowHead(IntPtr h)
        {
            int n = WindowNumber(h);
            string head = "Window " + (n > 0 ? "w" + n + " " : "") + "\"" + Clean(Title(h), 100) + "\" - " + ProcName(PidOf(h));
            if (!Native.IsIconic(h))
            {
                List<ScreenInfo> screens = Screens();
                int on = ScreenOf(h, screens);
                if (on == 0) head += " (on no screen: focus moves it back)";
                else if (screens.Count > 1) head += " on " + ScreenText(on, screens[on - 1]);
            }
            if (h == Native.GetForegroundWindow()) head += " (foreground)";
            if (Guarded(h)) head += " (Blackwall itself: read only)";
            return head;
        }

        static string Snapshot(Dictionary<string, object> r)
        {
            bool offscreen = Bool(r, "offscreen");
            if (Has(r, "id"))
            {
                AutomationElement root = ById(Int(r, "id", 0));
                return SnapshotText(root, IntPtr.Zero, "Part of the window: " + Label(root), MAX_LINES, offscreen);
            }
            IntPtr hwnd = DefaultWindow(r);
            string head = WindowHead(hwnd);
            if (Native.IsIconic(hwnd))
                return head + "\nThe window is minimized, so none of its controls are on screen. Use focus to restore it.";
            return SnapshotText(null, hwnd, head, MAX_LINES, offscreen);
        }

        // web_snapshot (2026-11-18): only the page inside a Chrome/Edge window, without the tab strip and
        // the address bar. The page is the Document control the browser exposes; its value is the URL.
        static string WebSnapshot(Dictionary<string, object> r)
        {
            IntPtr hwnd = DefaultWindow(r);
            string head = WindowHead(hwnd);
            if (Native.IsIconic(hwnd))
                return head + "\nThe window is minimized. Use focus to restore it.";
            AutomationElement doc = null;
            Thread t = new Thread(delegate()
            {
                try
                {
                    AutomationElement w = AutomationElement.FromHandle(hwnd);
                    AutomationElementCollection docs = w.FindAll(TreeScope.Descendants,
                        new PropertyCondition(AutomationElement.ControlTypeProperty, ControlType.Document));
                    foreach (AutomationElement d in docs)
                    {
                        Rect b = d.Current.BoundingRectangle;
                        if (b.IsEmpty || b.Width < 20 || b.Height < 20 || d.Current.IsOffscreen) continue;
                        if (doc == null || b.Width * b.Height > doc.Current.BoundingRectangle.Width * doc.Current.BoundingRectangle.Height) doc = d;
                    }
                }
                catch (Exception) { }
            });
            t.IsBackground = true;
            t.Start();
            t.Join(SNAPSHOT_BUDGET_MS);
            if (doc == null)
                throw new UserError("Error: no web page found in " + head + ". web_snapshot reads Chrome, Edge and other Chromium"
                    + " windows; Firefox does not list its page here. Use snapshot or ocr instead.");
            string url = "";
            try { object vp; if (doc.TryGetCurrentPattern(ValuePattern.Pattern, out vp)) url = Clean(((ValuePattern)vp).Current.Value, 300); } catch (Exception) { }
            string title = "";
            try { title = Clean(doc.Current.Name, 200); } catch (Exception) { }
            string h = "Web page in " + head + (title.Length > 0 ? "\nTitle: " + title : "") + (url.Length > 0 ? "\nURL: " + url : "")
                + "\n(Only the page. Click by id; links and buttons off screen are scrolled into view first. Fill a box with set_value.)";
            return SnapshotText(doc, IntPtr.Zero, h, MAX_LINES, Bool(r, "offscreen"));
        }

        static string SnapshotText(AutomationElement root, IntPtr hwnd, string head, int maxLines, bool offscreen)
        {
            return SnapshotText(root, hwnd, head, maxLines, offscreen, null);
        }

        // boxes (look): filled with the place of every listed control. Only the walking thread adds,
        // under the lock and before Abandoned is set, so the list is settled once this returns.
        static string SnapshotText(AutomationElement root, IntPtr hwnd, string head, int maxLines, bool offscreen, List<int[]> boxes)
        {
            Walk w = new Walk();
            w.Cache = NewCache();
            w.MaxLines = maxLines;
            w.IncludeOffscreen = offscreen;
            if (boxes != null)
            {
                w.Cache.Add(AutomationElement.BoundingRectangleProperty);
                w.Boxes = boxes;
            }
            AutomationElement start = root;
            IntPtr startHwnd = hwnd;
            Thread t = new Thread(delegate()
            {
                try
                {
                    AutomationElement e = start ?? AutomationElement.FromHandle(startHwnd);
                    WalkChildren(e, 0, w);
                }
                catch (Exception ex) { lock (w) { if (w.Reason.Length == 0) w.Reason = "error:" + ex.Message; } }
            });
            t.IsBackground = true;
            t.Start();
            bool done = t.Join(SNAPSHOT_BUDGET_MS + 3000);
            string body;
            int lines;
            lock (w)
            {
                if (!done) w.Abandoned = true;
                body = w.Out.ToString();
                lines = w.Lines;
            }
            StringBuilder sb = new StringBuilder(head).Append('\n');
            if (lines == 0) sb.Append("(no controls listed; if the window clearly shows something, try ocr)\n");
            sb.Append(body);
            if (!done)
                sb.Append("... the application stopped answering after ").Append(lines).Append(" controls (it may be busy,")
                  .Append(" or waiting on a dialog it opened). Answer or close that dialog, or wait a little and snapshot again.\n");
            else if (w.Reason == "time")
                sb.Append("... the application answered slowly, so the list stops after ").Append(lines)
                  .Append(" controls. Snapshot one part with id=<container id>, or snapshot again.\n");
            else if (w.Truncated)
                sb.Append("... stopped after ").Append(lines).Append(" controls. Snapshot one part with id=<container id>.\n");
            else if (w.Reason.StartsWith("error:"))
                sb.Append("... could not read the window: ").Append(w.Reason.Substring(6)).Append('\n');
            if (w.Offscreen > 0)
                sb.Append("(").Append(w.Offscreen).Append(" off-screen controls are not listed; scroll, or snapshot with offscreen=true)\n");
            return sb.ToString().TrimEnd();
        }

        // ---- read: the whole text of a control or a window -----------------------------------

        static string ReadText(Dictionary<string, object> r)
        {
            AutomationElement el = null;
            IntPtr hwnd = IntPtr.Zero;
            string head;
            if (Has(r, "id")) { el = ById(Int(r, "id", 0)); head = "Text of " + Label(el); }
            else { hwnd = DefaultWindow(r); head = "Text of " + WindowHead(hwnd); }
            StringBuilder outText = new StringBuilder();
            string err = null;
            AutomationElement start = el;
            IntPtr startHwnd = hwnd;
            Thread t = new Thread(delegate()
            {
                try
                {
                    AutomationElement e = start ?? AutomationElement.FromHandle(startHwnd);
                    CollectText(e, outText);
                }
                catch (Exception ex) { err = ex.Message; }
            });
            t.IsBackground = true;
            t.Start();
            bool done = t.Join(SNAPSHOT_BUDGET_MS + 3000);
            string text;
            lock (outText) { text = outText.ToString(); }
            StringBuilder sb = new StringBuilder(head).Append('\n');
            if (text.Trim().Length == 0) sb.Append("(no text; if the window clearly shows text, try ocr)");
            else sb.Append(text.TrimEnd());
            if (text.Length >= READ_CHARS) sb.Append("\n... (stopped at ").Append(READ_CHARS).Append(" characters)");
            if (!done) sb.Append("\n... the application stopped answering, so this may be incomplete.");
            else if (err != null) sb.Append("\n... could not read everything: ").Append(err);
            return sb.ToString();
        }

        static void AppendText(StringBuilder sb, string text)
        {
            if (string.IsNullOrEmpty(text)) return;
            lock (sb)
            {
                if (sb.Length >= READ_CHARS) return;
                string t = text.Replace("\r\n", "\n").Trim();
                if (t.Length == 0) return;
                if (sb.Length + t.Length > READ_CHARS) t = t.Substring(0, READ_CHARS - sb.Length);
                sb.Append(t).Append('\n');
            }
        }

        // A document gives its full text through TextPattern. Anything else is read as its parts,
        // in order: texts, values, and the names of items.
        static void CollectText(AutomationElement e, StringBuilder sb)
        {
            object tp;
            if (e.TryGetCurrentPattern(TextPattern.Pattern, out tp))
            {
                AppendText(sb, ((TextPattern)tp).DocumentRange.GetText(READ_CHARS));
                return;
            }
            CacheRequest c = NewCache();
            c.TreeScope = TreeScope.Element | TreeScope.Descendants;
            AutomationElement root;
            using (c.Activate()) { root = e.GetUpdatedCache(c); }
            string last = null;
            ReadTree(root, sb, ref last);
        }

        static void ReadTree(AutomationElement e, StringBuilder sb, ref string last)
        {
            AutomationElementCollection kids;
            try { kids = e.CachedChildren; } catch (Exception) { return; }
            foreach (AutomationElement k in kids)
            {
                if (sb.Length >= READ_CHARS) return;
                if (CBool(k, AutomationElement.IsOffscreenProperty)) { ReadTree(k, sb, ref last); continue; }
                ControlType ct = CP(k, AutomationElement.ControlTypeProperty) as ControlType;
                string piece = null;
                if (CBool(k, AutomationElement.IsTextPatternAvailableProperty) && (ct == ControlType.Document || ct == ControlType.Edit))
                {
                    object tp;
                    try
                    {
                        if (k.TryGetCurrentPattern(TextPattern.Pattern, out tp))
                        {
                            AppendText(sb, ((TextPattern)tp).DocumentRange.GetText(READ_CHARS));
                            continue;
                        }
                    }
                    catch (Exception) { }
                }
                object v = CP(k, ValuePattern.ValueProperty);
                string name = CStr(k, AutomationElement.NameProperty);
                if (v != null && Convert.ToString(v).Trim().Length > 0 && !CBool(k, AutomationElement.IsPasswordProperty))
                    piece = (name.Length > 0 ? name + ": " : "") + Convert.ToString(v);
                else if (name.Trim().Length > 0 && ct != ControlType.Window && ct != ControlType.Pane && ct != ControlType.ScrollBar)
                    piece = name;
                if (piece != null && piece != last) { AppendText(sb, piece); last = piece; }
                if (ct != ControlType.ScrollBar) ReadTree(k, sb, ref last);
            }
        }

        // ---- ocr: the text on the screen, for windows that expose nothing to UI Automation ---

        static string Ocr(Dictionary<string, object> r)
        {
#if BW_OCR
            System.Drawing.Bitmap bmp;
            int offX = 0, offY = 0;
            string head;
            if (Has(r, "path"))
            {
                string path = Str(r, "path");
                if (!System.IO.File.Exists(path)) throw new UserError("Error: there is no image at '" + path + "'.");
                bmp = new System.Drawing.Bitmap(path);
                head = "OCR of the image " + path + " (" + bmp.Width + "x" + bmp.Height + "; positions are pixels in the image)";
            }
            else
            {
                IntPtr h = DefaultWindow(r);
                bmp = CaptureWindow(h, out offX, out offY);
                head = "OCR of " + WindowHead(h) + " (positions are screen pixels: click with x and y)";
            }
            using (bmp)
            {
                return head + "\n" + RunOcr(bmp, offX, offY);
            }
#else
            throw new UserError("Error: OCR is not available: this Windows lacks the WinRT parts it needs. Use snapshot or read.");
#endif
        }

        // ---- look: a picture of the window with the snapshot ids drawn on it ------------------
        // The first line is for the tool, not the model: "picture <left> <top> <width> <height> <marked>".
        static string Look(Dictionary<string, object> r)
        {
#if BW_LOOK
            string outPath = Str(r, "out");
            if (outPath.Length == 0) throw new UserError("Error: look needs out (where to save the picture).");
            if (Has(r, "x0") && Has(r, "y0") && Has(r, "x1") && Has(r, "y1"))
                return LookRegion(Int(r, "x0", 0), Int(r, "y0", 0), Int(r, "x1", 0), Int(r, "y1", 0), outPath);
            if (Has(r, "screen")) return LookScreen(Int(r, "screen", 1), outPath);
            IntPtr h = DefaultWindow(r);
            int left, top;
            using (System.Drawing.Bitmap bmp = CaptureWindow(h, out left, out top))
            {
                List<int[]> boxes = new List<int[]>();
                string list = SnapshotText(null, h, WindowHead(h), MAX_LINES, false, boxes);
                Opaque(bmp);
                int marked = DrawMarks(bmp, boxes, left, top);
                bmp.Save(outPath, System.Drawing.Imaging.ImageFormat.Png);
                return "picture " + left + " " + top + " " + bmp.Width + " " + bmp.Height + " " + marked + "\n" + list;
            }
#else
            throw new UserError("Error: look is not available on this Windows (the picture parts could not be built). Use snapshot or ocr.");
#endif
        }

#if BW_LOOK
        // A whole screen (s1 is the main one), with the windows on it listed front first.
        static string LookScreen(int n, string outPath)
        {
            List<ScreenInfo> all = Screens();
            if (n < 1 || n > all.Count) throw new UserError("Error: there is no screen s" + n + ". " + ScreensLine());
            RECT b = all[n - 1].Bounds;
            int w = b.Right - b.Left, ht = b.Bottom - b.Top;
            if (w <= 0 || ht <= 0) throw new UserError("Error: screen s" + n + " has no size.");
            using (System.Drawing.Bitmap shot = new System.Drawing.Bitmap(w, ht, System.Drawing.Imaging.PixelFormat.Format32bppArgb))
            {
                using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(shot))
                    g.CopyFromScreen(b.Left, b.Top, 0, 0, new System.Drawing.Size(w, ht));
                Opaque(shot);
                shot.Save(outPath, System.Drawing.Imaging.ImageFormat.Png);
            }
            windows.Clear();
            windows.AddRange(TopWindows());
            IntPtr fg = Native.GetForegroundWindow();
            StringBuilder sb = new StringBuilder("Screen " + ScreenText(n, all[n - 1]) + ". " + ScreensLine() + "\nWindows on it, front first:");
            int listed = 0;
            for (int i = 0; i < windows.Count; i++)
            {
                if (Native.IsIconic(windows[i]) || ScreenOf(windows[i], all) != n) continue;
                sb.Append("\n  ").Append(WindowLine(i + 1, windows[i], fg, all));
                listed++;
            }
            if (listed == 0) sb.Append(" (none)");
            return "picture " + b.Left + " " + b.Top + " " + w + " " + ht + " 0\n" + sb.ToString();
        }

        // A part of the desktop at full resolution (zoom into small text or icons of the last look).
        static string LookRegion(int x0, int y0, int x1, int y1, string outPath)
        {
            int w = x1 - x0, ht = y1 - y0;
            if (w <= 0 || ht <= 0) throw new UserError("Error: the region has no size.");
            using (System.Drawing.Bitmap shot = new System.Drawing.Bitmap(w, ht, System.Drawing.Imaging.PixelFormat.Format32bppArgb))
            {
                using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(shot))
                    g.CopyFromScreen(x0, y0, 0, 0, new System.Drawing.Size(w, ht));
                Opaque(shot);
                shot.Save(outPath, System.Drawing.Imaging.ImageFormat.Png);
            }
            return "picture " + x0 + " " + y0 + " " + w + " " + ht + " 0\nZoomed region " + x0 + "," + y0 + " to " + x1 + "," + y1
                + " (desktop pixels). rx and ry now point into this zoomed picture.";
        }

        // PrintWindow leaves the alpha byte at 0 for many windows; a PNG of that is see-through.
        static void Opaque(System.Drawing.Bitmap bmp)
        {
            System.Drawing.Rectangle all = new System.Drawing.Rectangle(0, 0, bmp.Width, bmp.Height);
            System.Drawing.Imaging.BitmapData data = bmp.LockBits(all, System.Drawing.Imaging.ImageLockMode.ReadWrite,
                System.Drawing.Imaging.PixelFormat.Format32bppArgb);
            try
            {
                int n = Math.Abs(data.Stride) * bmp.Height;
                byte[] px = new byte[n];
                Marshal.Copy(data.Scan0, px, 0, n);
                for (int i = 3; i < n; i += 4) px[i] = 255;
                Marshal.Copy(px, 0, data.Scan0, n);
            }
            finally { bmp.UnlockBits(data); }
        }

        static int DrawMarks(System.Drawing.Bitmap bmp, List<int[]> boxes, int left, int top)
        {
            int n = 0;
            System.Drawing.Color red = System.Drawing.Color.FromArgb(235, 225, 29, 72);
            using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(bmp))
            using (System.Drawing.Pen pen = new System.Drawing.Pen(red, 2f))
            using (System.Drawing.Font font = new System.Drawing.Font("Segoe UI", 12f, System.Drawing.FontStyle.Bold, System.Drawing.GraphicsUnit.Pixel))
            using (System.Drawing.SolidBrush back = new System.Drawing.SolidBrush(red))
            using (System.Drawing.SolidBrush fore = new System.Drawing.SolidBrush(System.Drawing.Color.White))
            {
                g.TextRenderingHint = System.Drawing.Text.TextRenderingHint.AntiAliasGridFit;
                foreach (int[] b in boxes)
                {
                    int x0 = b[1] - left, y0 = b[2] - top, x1 = b[3] - left, y1 = b[4] - top;
                    if (x1 <= 0 || y1 <= 0 || x0 >= bmp.Width || y0 >= bmp.Height) continue;
                    if (x1 - x0 < 4 || y1 - y0 < 4) continue;
                    if (x1 - x0 >= bmp.Width - 8 && y1 - y0 >= bmp.Height - 8) continue;   // the whole window
                    g.DrawRectangle(pen, x0, y0, x1 - x0 - 1, y1 - y0 - 1);
                    string label = b[0].ToString();
                    System.Drawing.SizeF sz = g.MeasureString(label, font);
                    float lx = Math.Max(0, x0);
                    float ly = y0 - sz.Height >= 0 ? y0 - sz.Height : Math.Max(0, y0);
                    g.FillRectangle(back, lx, ly, sz.Width, sz.Height);
                    g.DrawString(label, font, fore, lx, ly);
                    n++;
                }
            }
            return n;
        }
#endif

#if BW_OCR
        static bool Blank(System.Drawing.Bitmap b)
        {
            System.Drawing.Color first = b.GetPixel(0, 0);
            for (int y = 0; y < b.Height; y += Math.Max(1, b.Height / 24))
                for (int x = 0; x < b.Width; x += Math.Max(1, b.Width / 24))
                    if (b.GetPixel(x, y) != first) return false;
            return true;
        }

        static System.Drawing.Bitmap CaptureWindow(IntPtr h, out int left, out int top)
        {
            if (Native.IsIconic(h)) { Native.ShowWindow(h, 9); Thread.Sleep(300); }
            RECT rc;
            if (!Native.GetWindowRect(h, out rc) || rc.Right <= rc.Left || rc.Bottom <= rc.Top)
                throw new UserError("Error: the window has no place on screen.");
            left = rc.Left; top = rc.Top;
            int w = rc.Right - rc.Left, ht = rc.Bottom - rc.Top;
            System.Drawing.Bitmap bmp = new System.Drawing.Bitmap(w, ht, System.Drawing.Imaging.PixelFormat.Format32bppArgb);
            bool ok;
            using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(bmp))
            {
                IntPtr hdc = g.GetHdc();
                ok = Native.PrintWindow(h, hdc, 2);   // PW_RENDERFULLCONTENT: also windows drawn by the GPU
                g.ReleaseHdc(hdc);
            }
            if (!ok || Blank(bmp))
            {
                // Some windows do not paint for PrintWindow. Read the screen instead (window in front).
                Activate(h);
                Thread.Sleep(250);
                using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(bmp))
                    g.CopyFromScreen(rc.Left, rc.Top, 0, 0, new System.Drawing.Size(w, ht));
            }
            return bmp;
        }

        static bool Cjk(char c)
        {
            return (c >= 0x3000 && c <= 0x30ff) || (c >= 0x3400 && c <= 0x9fff) || (c >= 0xf900 && c <= 0xfaff) || (c >= 0xff00 && c <= 0xffef);
        }

        static string RunOcr(System.Drawing.Bitmap src, int offX, int offY)
        {
            OcrEngine engine = OcrEngine.TryCreateFromUserProfileLanguages();
            if (engine == null)
                throw new UserError("Error: no OCR language is installed on this Windows (Settings > Time & language > Language & region).");
            // Small text reads better enlarged; stay inside the engine's limit.
            double scale = 1.0;
            int maxDim = (int)OcrEngine.MaxImageDimension;
            int longest = Math.Max(src.Width, src.Height);
            if (longest * 2 <= maxDim && longest < 2000) scale = 2.0;
            else if (longest > maxDim) scale = (double)maxDim / longest;
            System.Drawing.Bitmap img = src;
            if (scale != 1.0)
            {
                img = new System.Drawing.Bitmap(Math.Max(1, (int)(src.Width * scale)), Math.Max(1, (int)(src.Height * scale)));
                using (System.Drawing.Graphics g = System.Drawing.Graphics.FromImage(img))
                {
                    g.InterpolationMode = System.Drawing.Drawing2D.InterpolationMode.HighQualityBicubic;
                    g.DrawImage(src, 0, 0, img.Width, img.Height);
                }
            }
            // No System.Runtime.WindowsRuntime helpers (AsTask, AsRandomAccessStream): they need the
            // unified Windows.winmd of the SDK, which user machines do not have. The pixels go in
            // as a buffer, and the one asynchronous call is waited on by its status.
            OcrResult result;
            try
            {
                System.Drawing.Rectangle all = new System.Drawing.Rectangle(0, 0, img.Width, img.Height);
                System.Drawing.Bitmap argb = img.PixelFormat == System.Drawing.Imaging.PixelFormat.Format32bppArgb
                    ? img : img.Clone(all, System.Drawing.Imaging.PixelFormat.Format32bppArgb);
                byte[] pixels = new byte[img.Width * img.Height * 4];
                System.Drawing.Imaging.BitmapData data = argb.LockBits(all, System.Drawing.Imaging.ImageLockMode.ReadOnly,
                    System.Drawing.Imaging.PixelFormat.Format32bppArgb);
                try
                {
                    for (int row = 0; row < img.Height; row++)
                        Marshal.Copy(new IntPtr(data.Scan0.ToInt64() + (long)row * data.Stride), pixels, row * img.Width * 4, img.Width * 4);
                }
                finally { argb.UnlockBits(data); if (!object.ReferenceEquals(argb, img)) argb.Dispose(); }
                DataWriter writer = new DataWriter();
                writer.WriteBytes(pixels);
                IBuffer buffer = writer.DetachBuffer();
                SoftwareBitmap sbmp = SoftwareBitmap.CreateCopyFromBuffer(buffer, BitmapPixelFormat.Bgra8, img.Width, img.Height);
                global::Windows.Foundation.IAsyncOperation<OcrResult> op = engine.RecognizeAsync(sbmp);
                Stopwatch wait = Stopwatch.StartNew();
                while (op.Status == global::Windows.Foundation.AsyncStatus.Started)
                {
                    if (wait.ElapsedMilliseconds > 30000) { op.Cancel(); throw new UserError("Error: OCR took over 30 s."); }
                    Thread.Sleep(20);
                }
                if (op.Status != global::Windows.Foundation.AsyncStatus.Completed)
                    throw new UserError("Error: OCR failed (" + op.Status + (op.ErrorCode != null ? ": " + op.ErrorCode.Message : "") + ").");
                result = op.GetResults();
            }
            finally { if (!object.ReferenceEquals(img, src)) img.Dispose(); }
            List<OcrPiece> found = new List<OcrPiece>();
            foreach (OcrLine line in result.Lines)
            {
                // A line is split where the gap between words is wide: menu items and buttons on one
                // row then come out as separate pieces, each with its own position.
                List<OcrPiece> parts = new List<OcrPiece>();
                StringBuilder piece = new StringBuilder();
                double x0 = 0, y0 = 0, x1 = 0, y1 = 0, prevRight = 0, prevH = 0;
                char prevLast = ' ';
                bool open = false;
                foreach (OcrWord word in line.Words)
                {
                    global::Windows.Foundation.Rect b = word.BoundingRect;
                    double gap = b.X - prevRight;
                    if (open && gap > Math.Max(prevH * 1.2, 12 * scale))
                    {
                        parts.Add(Piece(piece.ToString(), x0, y0, x1, y1, scale, offX, offY));
                        piece.Length = 0; open = false;
                    }
                    string wt = word.Text;
                    if (!open) { x0 = b.X; y0 = b.Y; x1 = b.X + b.Width; y1 = b.Y + b.Height; open = true; }
                    else
                    {
                        if (!(Cjk(prevLast) && wt.Length > 0 && Cjk(wt[0]))) piece.Append(' ');
                        x0 = Math.Min(x0, b.X); y0 = Math.Min(y0, b.Y);
                        x1 = Math.Max(x1, b.X + b.Width); y1 = Math.Max(y1, b.Y + b.Height);
                    }
                    piece.Append(wt);
                    if (wt.Length > 0) prevLast = wt[wt.Length - 1];
                    prevRight = b.X + b.Width; prevH = b.Height;
                }
                if (open) parts.Add(Piece(piece.ToString(), x0, y0, x1, y1, scale, offX, offY));
                foreach (OcrPiece pc in parts) found.Add(pc);
            }
            if (found.Count == 0) return "(no text was recognized)";
            // In reading order: rows from the top, each row left to right (the engine's own order
            // follows its text blocks, which can run down one column before the next).
            found.Sort(delegate(OcrPiece a, OcrPiece b) { return a.Y != b.Y ? a.Y.CompareTo(b.Y) : a.X.CompareTo(b.X); });
            StringBuilder sb = new StringBuilder();
            List<OcrPiece> rowItems = new List<OcrPiece>();
            int rows = 0;
            for (int i = 0; i <= found.Count; i++)
            {
                bool flush = i == found.Count || (rowItems.Count > 0 && found[i].Y - rowItems[0].Y > Math.Max(6, rowItems[0].H * 0.6));
                if (flush && rowItems.Count > 0)
                {
                    rowItems.Sort(delegate(OcrPiece a, OcrPiece b) { return a.X.CompareTo(b.X); });
                    List<string> texts = new List<string>();
                    foreach (OcrPiece pc in rowItems) texts.Add("\"" + pc.Text.Replace("\"", "'") + "\" @" + pc.X + "," + pc.Y);
                    sb.Append(string.Join("   ", texts.ToArray())).Append('\n');
                    rows++;
                    rowItems.Clear();
                }
                if (i < found.Count) rowItems.Add(found[i]);
            }
            return rows + " rows of text (\"text\" @x,y = its center):\n" + sb.ToString().TrimEnd();
        }

        class OcrPiece
        {
            public string Text;
            public int X, Y;
            public double H;
        }

        static OcrPiece Piece(string text, double x0, double y0, double x1, double y1, double scale, int offX, int offY)
        {
            OcrPiece p = new OcrPiece();
            p.Text = text;
            p.X = offX + (int)Math.Round((x0 + x1) / 2 / scale);
            p.Y = offY + (int)Math.Round((y0 + y1) / 2 / scale);
            p.H = (y1 - y0) / scale;
            return p;
        }
#endif

        // ---- mouse -----------------------------------------------------------------

        // False when Windows did not take the input (locked screen, no interactive desktop).
        static bool MouseButton(string button, bool dbl) { return MouseButton(button, dbl ? 2 : 1); }

        static uint DownFlag(string button)
        {
            switch ((button ?? "").ToLowerInvariant())
            {
                case "right": return 0x0008;
                case "middle": return 0x0020;
                default: return 0x0002;
            }
        }

        // times = 1 (click), 2 (double), 3 (triple: selects a line or paragraph in most editors).
        static bool MouseButton(string button, int times)
        {
            uint down, up;
            switch ((button ?? "").ToLowerInvariant())
            {
                case "right": down = 0x0008; up = 0x0010; break;
                case "middle": down = 0x0020; up = 0x0040; break;
                default: down = 0x0002; up = 0x0004; break;
            }
            if (times < 1) times = 1;
            if (times > 3) times = 3;
            bool all = true;
            for (int i = 0; i < times; i++)
            {
                INPUT[] inp = new INPUT[2];
                inp[0].type = INPUT_MOUSE; inp[0].u.mi.dwFlags = down;
                inp[1].type = INPUT_MOUSE; inp[1].u.mi.dwFlags = up;
                if (Native.SendInput(2, inp, Marshal.SizeOf(typeof(INPUT))) != 2) all = false;
                if (i < times - 1) Thread.Sleep(60);
            }
            return all;
        }

        const string NO_INPUT = "Windows did not accept the synthetic input (the screen may be locked, or this session has no interactive desktop)";

        static bool PointOf(AutomationElement e, out Point pt)
        {
            pt = new Point();
            try { if (e.TryGetClickablePoint(out pt)) return true; } catch (Exception) { }
            Rect rc = e.Current.BoundingRectangle;
            if (rc.IsEmpty || rc.Width <= 0 || rc.Height <= 0) return false;
            pt = new Point(rc.Left + rc.Width / 2, rc.Top + rc.Height / 2);
            return true;
        }

        static bool Inside(AutomationElement hit, AutomationElement target)
        {
            TreeWalker walker = TreeWalker.RawViewWalker;
            AutomationElement cur = hit;
            for (int i = 0; i < 40 && cur != null; i++)
            {
                if (Automation.Compare(cur, target)) return true;
                try { cur = walker.GetParent(cur); } catch (Exception) { return false; }
            }
            return false;
        }

        // A click without the mouse, for controls that have no place on screen or are covered.
        static string InvokeByPattern(AutomationElement e)
        {
            object p;
            if (e.TryGetCurrentPattern(InvokePattern.Pattern, out p)) { ((InvokePattern)p).Invoke(); return "Invoke"; }
            if (e.TryGetCurrentPattern(TogglePattern.Pattern, out p)) { ((TogglePattern)p).Toggle(); return "Toggle"; }
            if (e.TryGetCurrentPattern(SelectionItemPattern.Pattern, out p)) { ((SelectionItemPattern)p).Select(); return "Select"; }
            if (e.TryGetCurrentPattern(ExpandCollapsePattern.Pattern, out p))
            {
                ExpandCollapsePattern ec = (ExpandCollapsePattern)p;
                if (ec.Current.ExpandCollapseState == ExpandCollapseState.Collapsed) ec.Expand(); else ec.Collapse();
                return "ExpandCollapse";
            }
            return null;
        }

        // Invoke and friends can block until a modal dialog that they opened is closed
        // (WinForms runs the click synchronously). Run them on a side thread and stop waiting.
        static string PatternClick(AutomationElement el, out bool stillRunning)
        {
            string via = null;
            Exception err = null;
            Thread t = new Thread(delegate()
            {
                try { via = InvokeByPattern(el); } catch (Exception e) { err = e; }
            });
            t.IsBackground = true;
            t.Start();
            stillRunning = !t.Join(5000);
            if (stillRunning) return "a UI Automation action";
            if (err != null) throw err;
            return via;
        }

        // Real clicks move the user's pointer. Put it back where the user left it.
        static bool RealClick(Point pt, string button, bool dbl) { return RealClick(pt, button, dbl ? 2 : 1); }

        static bool RealClick(Point pt, string button, int times)
        {
            POINT saved;
            bool haveSaved = Native.GetCursorPos(out saved);
#if BW_OVERLAY
            if (Overlay.Enabled) { Overlay.Say(null, (int)pt.X, (int)pt.Y, true); Thread.Sleep(180); }   // 押す場所を見せてから押す
#endif
            Native.SetCursorPos((int)pt.X, (int)pt.Y);
            Thread.Sleep(30);
            bool ok = MouseButton(button, times);
            Thread.Sleep(80);
            POINT now;
            if (haveSaved && Native.GetCursorPos(out now) && now.X == (int)pt.X && now.Y == (int)pt.Y)
                Native.SetCursorPos(saved.X, saved.Y);
            return ok;
        }

        // Pressing without the mouse, as a fallback. Null when the control has no such action.
        static string PressWithoutMouse(AutomationElement el, string label, string why)
        {
            bool running;
            string via = PatternClick(el, out running);
            if (via == null) return null;
            Thread.Sleep(300);
            return "Clicked " + label + " without the mouse (" + via + "; " + why + ")."
                + (running ? " It has not returned yet: it probably opened a dialog, and the application may not answer"
                    + " snapshots until that dialog is answered." : "")
                + After();
        }

        // A part of a web page (or a long list) below the visible area: ask it to scroll itself into view
        // before the mouse goes there (ScrollItemPattern). Nothing happens for parts already on screen.
        static void ScrollIntoViewIfOff(AutomationElement el)
        {
            try
            {
                if (!el.Current.IsOffscreen) return;
                object p;
                if (el.TryGetCurrentPattern(ScrollItemPattern.Pattern, out p))
                {
                    ((ScrollItemPattern)p).ScrollIntoView();
                    Thread.Sleep(250);
                }
            }
            catch (Exception) { }
        }

        static string Click(Dictionary<string, object> r)
        {
            string button = Str(r, "button");
            int times = Bool(r, "double") ? 2 : 1;
            if (Has(r, "count")) times = Math.Max(1, Math.Min(3, Int(r, "count", 1)));
            bool dbl = times > 1;
            string verb = times == 3 ? "Triple-clicked" : times == 2 ? "Double-clicked" : "Clicked";
            bool plain = !dbl && (button.Length == 0 || button.ToLowerInvariant() == "left");
            Point pt;
            if (Has(r, "id"))
            {
                AutomationElement el = ById(Int(r, "id", 0));
                string label = Label(el);
                // The real mouse by default (2026-10-05). 0.1.277 pressed through UI Automation first,
                // but Invoke does not return while a modal dialog it opened is up, and on the real
                // machine the app then stopped answering UI Automation at all: the next snapshots
                // took 16-24 s and listed nothing. Patterns remain the fallback below.
                ActivateFor(r, el);
                ScrollIntoViewIfOff(el);
                if (!PointOf(el, out pt))
                {
                    string done = plain ? PressWithoutMouse(el, label, "it has no place on screen") : null;
                    if (done != null) return done;
                    throw new UserError("Error: " + label + " has no place on screen. Scroll it into view, or click its parent.");
                }
                string warn = "";
                POINT ppt; ppt.X = (int)pt.X; ppt.Y = (int)pt.Y;
                if (Native.MonitorFromPoint(ppt, 0) == IntPtr.Zero)
                {
                    string done = plain ? PressWithoutMouse(el, label, "its place is on no screen") : null;
                    if (done != null) return done;
                    throw new UserError("Error: " + label + " is at " + ppt.X + "," + ppt.Y + ", which is on no screen. " + ScreensLine()
                        + " Use focus on its window to bring the window onto a screen.");
                }
                bool coveredBySelf = Guarded(Native.WindowFromPoint(ppt));
                AutomationElement hit = null;
                try { hit = AutomationElement.FromPoint(pt); } catch (Exception) { }
                if (coveredBySelf || (hit != null && !Inside(hit, el)))
                {
                    string by = coveredBySelf ? "Blackwall's own window" : Label(hit);
                    string done = plain ? PressWithoutMouse(el, label, "the spot on screen is covered by " + by) : null;
                    if (done != null) return done;
                    if (coveredBySelf)
                        throw new UserError("Error: not done: " + label + " is covered by Blackwall's own window, so the mouse would"
                            + " click Blackwall. Bring the target window to the front first (focus), or move it away from Blackwall.");
                    warn = " Warning: that spot is covered by " + Label(hit) + "; the click went there.";
                }
                CheckPoint(ppt.X, ppt.Y, "click");
                if (!RealClick(pt, button, times))
                {
                    string done = plain ? PressWithoutMouse(el, label, NO_INPUT) : null;
                    if (done != null) return done;
                    throw new UserError("Error: " + NO_INPUT + ".");
                }
                Thread.Sleep(350);
                return verb + " " + label + " with the mouse at " + (int)pt.X + "," + (int)pt.Y
                    + "." + warn + After();
            }
            if (Has(r, "x") && Has(r, "y"))
            {
                ActivateFor(r, null);
                GuardForeground(r, "click");
                int x = Int(r, "x", 0), y = Int(r, "y", 0);
                CheckPoint(x, y, "click");
                if (!RealClick(new Point(x, y), button, times)) throw new UserError("Error: " + NO_INPUT + ".");
                Thread.Sleep(350);
                string what = "";
                try { what = " on " + Label(AutomationElement.FromPoint(new Point(x, y))); } catch (Exception) { }
                return verb + " at " + x + "," + y + what + "." + After();
            }
            throw new UserError("Error: click needs id (from snapshot), or x and y.");
        }

        static string ScrollWithoutMouse(AutomationElement from, int amount)
        {
            AutomationElement cur = from;
            for (int i = 0; i < 15 && cur != null; i++)
            {
                object p;
                if (cur.TryGetCurrentPattern(ScrollPattern.Pattern, out p) && ((ScrollPattern)p).Current.VerticallyScrollable)
                {
                    ScrollPattern sp = (ScrollPattern)p;
                    ScrollAmount dir = amount < 0 ? ScrollAmount.SmallIncrement : ScrollAmount.SmallDecrement;
                    for (int k = 0; k < Math.Abs(amount) * 3; k++) sp.ScrollVertical(dir);
                    Thread.Sleep(200);
                    return "Scrolled " + Label(cur) + (amount < 0 ? " down " + (-amount) : " up " + amount)
                        + " notches (without the mouse)." + After();
                }
                try { cur = TreeWalker.ControlViewWalker.GetParent(cur); } catch (Exception) { cur = null; }
                if (cur != null && Automation.Compare(cur, AutomationElement.RootElement)) break;
            }
            return null;
        }

        static string Scroll(Dictionary<string, object> r)
        {
            int amount = Int(r, "amount", -5);
            if (amount == 0) amount = -5;
            int x, y;
            if (Has(r, "id"))
            {
                AutomationElement el = ById(Int(r, "id", 0));
                ActivateFor(r, el);
                Rect rc = el.Current.BoundingRectangle;
                if (rc.IsEmpty || rc.Width <= 0 || rc.Height <= 0)
                {
                    string done = ScrollWithoutMouse(el, amount);
                    if (done != null) return done;
                    throw new UserError("Error: " + Label(el) + " has no place on screen and nothing around it scrolls.");
                }
                x = (int)(rc.Left + rc.Width / 2); y = (int)(rc.Top + rc.Height / 2);
                POINT sp; sp.X = x; sp.Y = y;
                if (Native.MonitorFromPoint(sp, 0) == IntPtr.Zero || Guarded(Native.WindowFromPoint(sp)))
                {
                    string done = ScrollWithoutMouse(el, amount);
                    if (done != null) return done;
                }
            }
            else if (Has(r, "x") && Has(r, "y"))
            {
                ActivateFor(r, null);
                x = Int(r, "x", 0); y = Int(r, "y", 0);
            }
            else
            {
                // The middle of the named window, or of the window Blackwall last worked in.
                IntPtr h = Has(r, "window") ? ResolveWindow(Str(r, "window"), Num(r, "wait", 0))
                    : (lastTop != IntPtr.Zero && Native.IsWindow(lastTop) ? lastTop : IntPtr.Zero);
                if (h == IntPtr.Zero) throw new UserError("Error: scroll needs id (from snapshot), window, or x and y.");
                RefuseSelf(h, "scroll");
                lastTop = h;
                Activate(h);
                RECT wr;
                if (!Native.GetWindowRect(h, out wr) || wr.Right <= wr.Left) throw new UserError("Error: the window has no place on screen.");
                x = (wr.Left + wr.Right) / 2; y = (wr.Top + wr.Bottom) / 2;
            }
            CheckPoint(x, y, "scroll");
            POINT saved;
            bool haveSaved = Native.GetCursorPos(out saved);
            Native.SetCursorPos(x, y);
            Thread.Sleep(30);
            INPUT[] inp = new INPUT[1];
            inp[0].type = INPUT_MOUSE;
            inp[0].u.mi.dwFlags = MOUSEEVENTF_WHEEL;
            inp[0].u.mi.mouseData = unchecked((uint)(amount * 120));
            uint sentWheel = Native.SendInput(1, inp, Marshal.SizeOf(typeof(INPUT)));
            Thread.Sleep(80);
            if (haveSaved) Native.SetCursorPos(saved.X, saved.Y);
            if (sentWheel != 1) throw new UserError("Error: " + NO_INPUT + ".");
            Thread.Sleep(220);
            return "Scrolled " + (amount < 0 ? "down " + (-amount) : "up " + amount) + " notches with the mouse wheel." + After();
        }

        // ---- pointer: move, drag, where it is ----------------------------------------------

        // A point from id (its center) or from x,y. prefix "from_" reads the start of a drag.
        static bool PointArg(Dictionary<string, object> r, string prefix, out int x, out int y, out string label)
        {
            x = 0; y = 0; label = "";
            if (Has(r, prefix + "id"))
            {
                AutomationElement el = ById(Int(r, prefix + "id", 0));
                label = Label(el);
                Point pt;
                if (!PointOf(el, out pt)) throw new UserError("Error: " + label + " has no place on screen.");
                x = (int)pt.X; y = (int)pt.Y;
                return true;
            }
            if (Has(r, prefix + "x") && Has(r, prefix + "y"))
            {
                x = Int(r, prefix + "x", 0); y = Int(r, prefix + "y", 0);
                return true;
            }
            return false;
        }

        static string HoverLabel(int x, int y)
        {
            try { return " over " + Label(AutomationElement.FromPoint(new Point(x, y))); } catch (Exception) { return ""; }
        }

        // Moves the pointer and leaves it there (hover menus, tooltips).
        static string MoveMouse(Dictionary<string, object> r)
        {
            int x, y; string label;
            if (!PointArg(r, "", out x, out y, out label)) throw new UserError("Error: move needs id, rx and ry, or x and y.");
            CheckPoint(x, y, "move");
            if (!Native.SetCursorPos(x, y)) throw new UserError("Error: " + NO_INPUT + ".");
            Thread.Sleep(400);
            return "Moved the mouse to " + x + "," + y + HoverLabel(x, y) + " (left there, so hover menus and tooltips stay open).";
        }

        static void Glide(int x0, int y0, int x1, int y1, int steps)
        {
            for (int i = 1; i <= steps; i++)
            {
                Native.SetCursorPos(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps);
                Thread.Sleep(15);
            }
        }

        // Press at the start, glide, release at the end (sliders, moving items, selecting a range).
        static string Drag(Dictionary<string, object> r)
        {
            int x0, y0, x1, y1; string l0, l1;
            if (!PointArg(r, "from_", out x0, out y0, out l0))
                throw new UserError("Error: drag needs a start (from_id, from_rx and from_ry, or from_x and from_y) and an end (id, rx and ry, or x and y).");
            if (!PointArg(r, "", out x1, out y1, out l1))
                throw new UserError("Error: drag needs an end: id, rx and ry, or x and y.");
            ActivateFor(r, null);
            CheckPoint(x0, y0, "drag");
#if BW_OVERLAY
            if (Overlay.Enabled) { Overlay.Say(null, x0, y0, true); Thread.Sleep(180); }
#endif
            CheckPoint(x1, y1, "drag");
            string button = Str(r, "button");
            uint down = DownFlag(button), up = down << 1;
            POINT saved;
            bool haveSaved = Native.GetCursorPos(out saved);
            Native.SetCursorPos(x0, y0);
            Thread.Sleep(60);
            INPUT[] d = new INPUT[1];
            d[0].type = INPUT_MOUSE; d[0].u.mi.dwFlags = down;
            bool ok = Native.SendInput(1, d, Marshal.SizeOf(typeof(INPUT))) == 1;
            Thread.Sleep(80);
            int dist = Math.Max(Math.Abs(x1 - x0), Math.Abs(y1 - y0));
            Glide(x0, y0, x1, y1, Math.Max(5, Math.Min(40, dist / 10)));
            Thread.Sleep(80);
            INPUT[] u = new INPUT[1];
            u[0].type = INPUT_MOUSE; u[0].u.mi.dwFlags = up;
            if (Native.SendInput(1, u, Marshal.SizeOf(typeof(INPUT))) != 1) ok = false;
            Thread.Sleep(120);
            if (haveSaved) Native.SetCursorPos(saved.X, saved.Y);
            if (!ok) throw new UserError("Error: " + NO_INPUT + ".");
            Thread.Sleep(250);
            return "Dragged from " + x0 + "," + y0 + (l0.Length > 0 ? " (" + l0 + ")" : "") + " to " + x1 + "," + y1
                + (l1.Length > 0 ? " (" + l1 + ")" : HoverLabel(x1, y1)) + "." + After();
        }

        static string CursorAt()
        {
            POINT p;
            if (!Native.GetCursorPos(out p)) throw new UserError("Error: the mouse position could not be read.");
            return "The mouse is at " + p.X + "," + p.Y + HoverLabel(p.X, p.Y) + ".";
        }

        // ---- keyboard ----------------------------------------------------------------

        static INPUT Key(ushort vk, bool up)
        {
            INPUT i = new INPUT();
            i.type = INPUT_KEYBOARD;
            i.u.ki.wVk = vk;
            i.u.ki.wScan = (ushort)Native.MapVirtualKeyW(vk, 0);
            uint flags = up ? KEYEVENTF_KEYUP : 0;
            if (Extended(vk)) flags |= KEYEVENTF_EXTENDEDKEY;
            i.u.ki.dwFlags = flags;
            return i;
        }

        static bool Extended(ushort vk)
        {
            return (vk >= 0x21 && vk <= 0x28) || vk == 0x2D || vk == 0x2E || vk == 0x5B || vk == 0x5C
                || vk == 0x5D || vk == 0x6F || vk == 0x90 || vk == 0x2C;
        }

        static void Send(List<INPUT> list)
        {
            const int chunk = 40;
            for (int i = 0; i < list.Count; i += chunk)
            {
                int n = Math.Min(chunk, list.Count - i);
                INPUT[] part = list.GetRange(i, n).ToArray();
                uint sent = Native.SendInput((uint)n, part, Marshal.SizeOf(typeof(INPUT)));
                if (sent != (uint)n)
                    throw new UserError("Error: " + NO_INPUT + (i > 0 ? "; only part of the input was sent." : ".")
                        + " For text boxes, set_value works without keystrokes.");
                Thread.Sleep(15);
            }
        }

        static readonly Dictionary<string, ushort> Named = MakeNamed();

        static Dictionary<string, ushort> MakeNamed()
        {
            Dictionary<string, ushort> d = new Dictionary<string, ushort>();
            d["enter"] = 0x0D; d["return"] = 0x0D; d["tab"] = 0x09; d["esc"] = 0x1B; d["escape"] = 0x1B;
            d["space"] = 0x20; d["backspace"] = 0x08; d["bs"] = 0x08; d["delete"] = 0x2E; d["del"] = 0x2E;
            d["insert"] = 0x2D; d["ins"] = 0x2D; d["home"] = 0x24; d["end"] = 0x23;
            d["pageup"] = 0x21; d["pgup"] = 0x21; d["pagedown"] = 0x22; d["pgdn"] = 0x22;
            d["left"] = 0x25; d["up"] = 0x26; d["right"] = 0x27; d["down"] = 0x28;
            d["capslock"] = 0x14; d["printscreen"] = 0x2C; d["prtsc"] = 0x2C; d["pause"] = 0x13;
            d["apps"] = 0x5D; d["menu"] = 0x5D; d["contextmenu"] = 0x5D; d["numlock"] = 0x90; d["scrolllock"] = 0x91;
            d["plus"] = 0xBB; d["minus"] = 0xBD; d["comma"] = 0xBC; d["period"] = 0xBE;
            d["ctrl"] = 0x11; d["control"] = 0x11; d["shift"] = 0x10; d["alt"] = 0x12;
            d["win"] = 0x5B; d["windows"] = 0x5B; d["meta"] = 0x5B; d["super"] = 0x5B; d["cmd"] = 0x5B;
            d["kanji"] = 0x19; d["henkan"] = 0x1C; d["muhenkan"] = 0x1D;
            for (int i = 1; i <= 24; i++) d["f" + i] = (ushort)(0x6F + i);
            return d;
        }

        static bool IsModifier(ushort vk)
        {
            return vk == 0x11 || vk == 0x10 || vk == 0x12 || vk == 0x5B;
        }

        static ushort VkOf(string name, out bool shift)
        {
            shift = false;
            ushort vk;
            string low = name.ToLowerInvariant();
            if (Named.TryGetValue(low, out vk)) return vk;
            if (name.Length == 1)
            {
                char c = name[0];
                if (c >= 'a' && c <= 'z') return (ushort)char.ToUpperInvariant(c);
                if (c >= 'A' && c <= 'Z') return (ushort)c;
                if (c >= '0' && c <= '9') return (ushort)c;
                short s = Native.VkKeyScanW(c);
                if (s != -1)
                {
                    shift = (s & 0x100) != 0;
                    return (ushort)(s & 0xff);
                }
            }
            throw new UserError("Error: unknown key '" + name + "'. Use names like enter, tab, esc, f5, ctrl+s, alt+f4, or type text with action type.");
        }

        static string PressCombo(string combo, List<INPUT> list)
        {
            string c = combo.Trim();
            if (c.EndsWith("++")) c = c.Substring(0, c.Length - 2) + "+plus";
            if (c == "+") c = "plus";
            string[] parts = c.Split('+');
            List<ushort> held = new List<ushort>();
            for (int i = 0; i < parts.Length - 1; i++)
            {
                bool sh;
                ushort m = VkOf(parts[i].Trim(), out sh);
                if (!IsModifier(m)) throw new UserError("Error: '" + parts[i] + "' in '" + combo + "' is not a modifier (ctrl, shift, alt, win).");
                held.Add(m);
            }
            bool needShift;
            ushort main = VkOf(parts[parts.Length - 1].Trim(), out needShift);
            if (needShift && !held.Contains(0x10)) held.Add(0x10);
            foreach (ushort m in held) list.Add(Key(m, false));
            list.Add(Key(main, false));
            list.Add(Key(main, true));
            for (int i = held.Count - 1; i >= 0; i--) list.Add(Key(held[i], true));
            return c;
        }

        static string Keys(Dictionary<string, object> r)
        {
            string keys = Str(r, "keys").Trim();
            if (keys.Length == 0) throw new UserError("Error: key needs keys, e.g. \"ctrl+s\" or \"tab tab enter\".");
            AutomationElement el = Has(r, "id") ? ById(Int(r, "id", 0)) : null;
            ActivateFor(r, el);
            TargetForKeys(r);
            GuardForeground(r, "keys");
            if (el != null) { try { el.SetFocus(); } catch (Exception) { } }
            RefuseSelfForeground("keys");
            List<INPUT> list = new List<INPUT>();
            List<string> done = new List<string>();
            double hold = Num(r, "hold", 0);
            if (hold > 0)
            {
                // Hold one key or combination down for some seconds (games, push-to-talk, auto-repeat).
                if (keys.IndexOf(' ') >= 0) throw new UserError("Error: hold works with one key or combination, not a sequence.");
                string c = PressCombo(keys, list);
                int half = list.Count / 2;
                Send(list.GetRange(0, half));
                Thread.Sleep((int)(Math.Min(hold, 10.0) * 1000));
                Send(list.GetRange(half, list.Count - half));
                Thread.Sleep(200);
                return "Held " + c + " for " + Math.Min(hold, 10.0).ToString(System.Globalization.CultureInfo.InvariantCulture) + " s." + After();
            }
            foreach (string combo in keys.Split(new char[] { ' ', ',' }, StringSplitOptions.RemoveEmptyEntries))
            {
                if (done.Count >= 50) throw new UserError("Error: too many keys at once (50 at most).");
                done.Add(PressCombo(combo, list));
            }
            Send(list);
            Thread.Sleep(300);
            return "Pressed " + string.Join(" ", done.ToArray()) + "." + After();
        }

        static void AddText(string text, List<INPUT> list)
        {
            for (int i = 0; i < text.Length; i++)
            {
                char c = text[i];
                if (c == '\r') continue;
                if (c == '\n') { list.Add(Key(0x0D, false)); list.Add(Key(0x0D, true)); continue; }
                if (c == '\t') { list.Add(Key(0x09, false)); list.Add(Key(0x09, true)); continue; }
                INPUT d = new INPUT();
                d.type = INPUT_KEYBOARD;
                d.u.ki.wScan = c;
                d.u.ki.dwFlags = KEYEVENTF_UNICODE;
                INPUT u = d;
                u.u.ki.dwFlags = KEYEVENTF_UNICODE | KEYEVENTF_KEYUP;
                list.Add(d);
                list.Add(u);
            }
        }

        static string TypeText(Dictionary<string, object> r)
        {
            string text = Str(r, "text");
            if (text.Length == 0) throw new UserError("Error: type needs text.");
            string target = "the focused control";
            ValuePattern watch = null;
            string before = null;
            if (Has(r, "id"))
            {
                AutomationElement el = ById(Int(r, "id", 0));
                target = Label(el);
                object vp;
                try
                {
                    if (!el.Current.IsPassword && el.TryGetCurrentPattern(ValuePattern.Pattern, out vp))
                    {
                        watch = (ValuePattern)vp;
                        before = watch.Current.Value;
                    }
                }
                catch (Exception) { watch = null; }
                ActivateFor(r, el);
                bool focused = false;
                try { el.SetFocus(); focused = true; } catch (Exception) { }
                if (!focused)
                {
                    Point pt;
                    if (!PointOf(el, out pt)) throw new UserError("Error: could not focus " + target + ". Click it first.");
                    CheckPoint((int)pt.X, (int)pt.Y, "click before typing");
                    if (!RealClick(pt, "left", false)) throw new UserError("Error: " + NO_INPUT + ".");
                }
                Thread.Sleep(100);
                RefuseSelfForeground("text");
            }
            else { ActivateFor(r, null); TargetForKeys(r); GuardForeground(r, "text"); RefuseSelfForeground("text"); }
            List<INPUT> list = new List<INPUT>();
            AddText(text, list);
            Send(list);
            Thread.Sleep(250);
            string note = "";
            if (watch != null)
            {
                try
                {
                    if (watch.Current.Value == before)
                        note = " Warning: its value did not change, so the text probably did not arrive (the window may be"
                            + " running as administrator, or the control ignores typing). set_value replaces the value directly.";
                }
                catch (Exception) { }
            }
            return "Typed " + text.Length + " characters into " + target + "." + note + After();
        }

        static string SetValue(Dictionary<string, object> r)
        {
            if (!Has(r, "id")) throw new UserError("Error: set_value needs id (an edit box or combo box from snapshot).");
            AutomationElement el = ById(Int(r, "id", 0));
            string label = Label(el);
            RefuseSelf(TopHandle(el), "set_value");
            object p;
            if (!el.TryGetCurrentPattern(ValuePattern.Pattern, out p))
                throw new UserError("Error: " + label + " does not take a value. Click it and use type instead.");
            ValuePattern vp = (ValuePattern)p;
            if (vp.Current.IsReadOnly) throw new UserError("Error: " + label + " is read-only.");
            IntPtr svTop = TopHandle(el);
            if (svTop != IntPtr.Zero) lastTop = svTop;
            vp.SetValue(Str(r, "text"));
            Thread.Sleep(200);
            string now = "";
            try { now = Clean(vp.Current.Value, 200); } catch (Exception) { }
            return "Set the value of " + label + ". It now reads \"" + now + "\"." + After();
        }

        // ---- focus / launch -----------------------------------------------------------

        static string Focus(Dictionary<string, object> r)
        {
            IntPtr h = ResolveWindow(Str(r, "window"), Num(r, "wait", 0));
            RefuseSelf(h, "focus");
            lastTop = h;
            string title = Clean(Title(h), 80);
            string moved = BringOntoScreen(h);
            if (!Activate(h)) throw new UserError("Error: Windows refused to bring \"" + title + "\" to the front. Click inside the window instead." + moved + After());
            return "Brought \"" + title + "\" to the front." + moved + After();
        }

        // A window left on a screen that is no longer there (unplugged, or moved past every edge)
        // cannot be seen or clicked. Put it on the main screen, keeping its size.
        static string BringOntoScreen(IntPtr h)
        {
            if (Native.IsIconic(h) || Native.MonitorFromWindow(h, 0) != IntPtr.Zero) return "";
            List<ScreenInfo> all = Screens();
            if (all.Count == 0) return "";
            RECT work = all[0].Work;
            // SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE
            if (!Native.SetWindowPos(h, IntPtr.Zero, work.Left + 40, work.Top + 40, 0, 0, 0x0001 | 0x0004 | 0x0010)) return "";
            return " It was on no screen, so it was moved onto s1 (the main screen).";
        }

        static string Launch(Dictionary<string, object> r)
        {
            string cmd = Str(r, "command").Trim();
            if (cmd.Length == 0) throw new UserError("Error: launch needs command (a program like notepad, a file path, or a URL).");
            double wait = Num(r, "wait", 10);
            if (wait < 0) wait = 0;
            if (wait > 60) wait = 60;
            HashSet<IntPtr> before = new HashSet<IntPtr>(TopWindows());
            ProcessStartInfo psi = new ProcessStartInfo(cmd, Str(r, "args"));
            psi.UseShellExecute = true;
            // A program started from its own folder finds its files the way it does when the user
            // double-clicks it.
            try
            {
                if (System.IO.File.Exists(cmd)) psi.WorkingDirectory = System.IO.Path.GetDirectoryName(System.IO.Path.GetFullPath(cmd));
                else if (Has(r, "cwd") && System.IO.Directory.Exists(Str(r, "cwd"))) psi.WorkingDirectory = Str(r, "cwd");
            }
            catch (Exception) { }
            Process p;
            try { p = Process.Start(psi); }
            catch (Exception e)
            {
                bool looksLikePath = cmd.IndexOf('\\') >= 0 || cmd.IndexOf('/') >= 0;
                string hint = "";
                try
                {
                    if (looksLikePath && !System.IO.File.Exists(cmd) && !System.IO.Directory.Exists(cmd))
                        hint = " There is no file at that path; find the real location first (for example with dir /s /b).";
                }
                catch (Exception) { }
                throw new UserError("Error: could not start '" + cmd + "': " + e.Message + hint);
            }
            int pid = 0;
            try { if (p != null) pid = p.Id; } catch (Exception) { }
            Stopwatch sw = Stopwatch.StartNew();
            while (sw.Elapsed.TotalSeconds < wait)
            {
                Thread.Sleep(300);
                IntPtr found = IntPtr.Zero;
                foreach (IntPtr h in TopWindows())
                {
                    if (before.Contains(h)) continue;
                    if (found == IntPtr.Zero || PidOf(h) == pid) found = h;
                }
                if (found != IntPtr.Zero)
                {
                    Thread.Sleep(400);
                    windows.Clear();
                    windows.AddRange(TopWindows());
                    int n = WindowNumber(found);
                    if (n == 0) { windows.Add(found); n = windows.Count; }
                    lastTop = found;
                    Activate(found);
                    return "Started " + cmd + ". New window " + WindowLine(n, found, Native.GetForegroundWindow(), Screens()) + "." + After();
                }
            }
            return "Started " + cmd + (pid > 0 ? " (pid " + pid + ")" : "") + ", but no new window appeared within " + wait
                + " s. It may still be loading (snapshot with window and wait), or it reused a window that was already open (windows).";
        }
    }
}
