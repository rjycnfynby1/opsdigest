// Experimental Windows shell; no browser extension or Chrome profile copying.
using System;
using System.IO;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

partial class Prototype : Form {
    readonly SplitContainer split = new SplitContainer { Dock = DockStyle.Fill };
    readonly TabControl tabs = new TabControl { Dock = DockStyle.Fill };
    readonly TextBox address = new TextBox { Dock = DockStyle.Top };
    readonly TextBox output = new TextBox { Dock = DockStyle.Bottom, Multiline = true, Height = 85, ScrollBars = ScrollBars.Both, ReadOnly = true };
    CoreWebView2Environment env;
    readonly string appUrl;
    readonly string smokeFile;
    bool rpaActive;
    string selection;
    System.Threading.Tasks.TaskCompletionSource<bool> selectionReady;
    readonly string picker = File.ReadAllText(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "picker.js"));

    [STAThread] static void Main(string[] args) {
        Application.EnableVisualStyles();
        Application.Run(new Prototype(args));
    }
    Prototype(string[] args) {
        appUrl = args.Length > 0 && args[0] != "--smoke" ? args[0] : null;
        smokeFile = args.Length > 1 && args[0] == "--smoke" ? args[1] : null;
        Text = "OpsDigest"; Width = 1440; Height = 900;
        Controls.Add(split); split.SplitterDistance = 400;
        address.AccessibleName = "Адрес портала";
        output.AccessibleName = "Результат действия или ошибка";
        MinimumSize = new System.Drawing.Size(900, 650);
        split.Panel2Collapsed = true;
        split.Panel2.Controls.Add(tabs); split.Panel2.Controls.Add(output);
        var bar = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 72, AutoScroll = true };
        Button add = new Button { Text = "+ вкладка" }, go = new Button { Text = "Открыть" }, read = new Button { Text = "Прочитать" }, expand = new Button { Text = "Развернуть" };
        bar.Controls.AddRange(new Control[] { add, go, read, expand });
        var back = new Button { Text = "Назад" };
        var refresh = new Button { Text = "Обновить" };
        var close = new Button { Text = "Закрыть вкладку", Width = 120 };
        var save = new Button { Text = "Сохранить элемент", Width = 145 };
        var hide = new Button { Text = "Скрыть браузер", Width = 130 };
        bar.Controls.Add(hide);
        hide.Click += (s,e) => { split.Panel1Collapsed = false; split.Panel2Collapsed = true; };
        bar.Controls.AddRange(new Control[] { back, refresh, close, save });
        bar.Controls.Add(new Label { Text = "Alt + наведение: подсветка; Alt + клик: выбрать без выполнения", AutoSize = true });
        back.Click += (s,e) => { if (Current().CanGoBack) Current().GoBack(); };
        refresh.Click += (s,e) => Current().Reload();
        close.Click += (s,e) => { if (running) { output.Text = "Сначала остановите сценарий"; return; } if (tabs.TabCount > 1) { var tab = tabs.SelectedTab; childFrames.Remove(Current()); tabs.TabPages.Remove(tab); tab.Dispose(); selection = null; } };
        save.Click += (s,e) => {
            if (selection == null) { output.Text = "Сначала выберите элемент через Alt + клик"; return; }
            using (var dialog = new SaveFileDialog { Filter = "Описание элемента (*.json)|*.json", FileName = "element.json" }) {
                if (dialog.ShowDialog() == DialogResult.OK) File.WriteAllText(dialog.FileName, selection);
            }
        };
        tabs.SelectedIndexChanged += (s,e) => { selection = null; if (tabs.SelectedTab != null && Current().Source != null) address.Text = Current().Source.ToString(); };
        split.Panel2.Controls.Add(address); split.Panel2.Controls.Add(bar);
        SetupScenario();
        add.Click += async (s,e) => { try { await AddTab("about:blank"); } catch(Exception ex) { output.Text = ex.Message; } };
        go.Click += (s,e) => { Uri uri; if (Uri.TryCreate(address.Text, UriKind.Absolute, out uri) && (uri.Scheme == "http" || uri.Scheme == "https")) Current().CoreWebView2.Navigate(uri.AbsoluteUri); else output.Text = "Введите адрес http:// или https://"; };
        read.Click += async (s,e) => { try { output.Text = await Current().CoreWebView2.ExecuteScriptAsync("document.body.innerText.slice(0,20000)"); } catch(Exception ex) { output.Text = ex.Message; } };
        expand.Click += (s,e) => { split.Panel1Collapsed = !split.Panel1Collapsed; };
        Shown += async (s,e) => { try { await Start(); } catch(Exception ex) { if (smokeFile != null) { File.WriteAllText(smokeFile, "FAIL: " + ex); Close(); } else output.Text = ex.ToString(); } };
    }
    WebView2 Current() { return (WebView2)tabs.SelectedTab.Controls[0]; }
    async System.Threading.Tasks.Task<WebView2> AddTab(string url) {
        var tab = new TabPage("Портал " + (tabs.TabCount + 1));
        var view = new WebView2 { Dock = DockStyle.Fill };
        tab.Controls.Add(view); tabs.TabPages.Add(tab); tabs.SelectedTab = tab;
        await view.EnsureCoreWebView2Async(env);
        childFrames[view] = new System.Collections.Generic.List<CoreWebView2Frame>();
        await view.CoreWebView2.AddScriptToExecuteOnDocumentCreatedAsync(picker);
        view.CoreWebView2.WebMessageReceived += (s,e) => ReceiveSelection(e.WebMessageAsJson);
        view.CoreWebView2.FrameCreated += (s,e) => {
            ConfigureFrame(view, e.Frame);
        };
        view.CoreWebView2.NavigationStarting += (s,e) => { selection = null; };
        view.CoreWebView2.DocumentTitleChanged += (s,e) => { tab.Text = view.CoreWebView2.DocumentTitle.Length > 24 ? view.CoreWebView2.DocumentTitle.Substring(0,24) : view.CoreWebView2.DocumentTitle; };
        view.CoreWebView2.SourceChanged += (s,e) => { if (Current() == view) address.Text = view.Source.ToString(); };
        view.CoreWebView2.NewWindowRequested += async (s,e) => {
            var wait = e.GetDeferral(); e.Handled = true;
            try { var child = await AddTab("about:blank"); e.NewWindow = child.CoreWebView2; }
            catch(Exception ex) { output.Text = ex.Message; }
            finally { wait.Complete(); }
        };
        view.CoreWebView2.Navigate(url);
        return view;
    }
    void ConfigureFrame(WebView2 view, CoreWebView2Frame frame) {
        childFrames[view].Add(frame);
        frame.WebMessageReceived += (s,e) => ReceiveSelection(e.WebMessageAsJson);
        frame.FrameCreated += (s,e) => ConfigureFrame(view, e.Frame);
    }
    void ReceiveSelection(string json) {
        // Portal messages are untrusted data. They never invoke host actions.
        if (json.Length > 20000) return;
        try {
            var data = new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<System.Collections.Generic.Dictionary<string,object>>(json);
            if (data == null || !data.ContainsKey("kind") || (string)data["kind"] != "element") return;
            var clean = new System.Collections.Generic.Dictionary<string,object>();
            clean["version"] = 1; clean["kind"] = "element";
            foreach (string key in new [] { "page", "tag", "role", "name", "frame", "text" }) {
                object value; string str = data.TryGetValue(key, out value) ? value as string : "";
                if (str == null || str.Length > 2200) return;
                clean[key] = str;
            }
            selection = new System.Web.Script.Serialization.JavaScriptSerializer().Serialize(clean);
            output.Text = selection;
            if (selectionReady != null) selectionReady.TrySetResult(true);
        } catch { output.Text = "Некорректное описание элемента"; }
    }
    async System.Threading.Tasks.Task Start() {
        string profile = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "OpsDigest", smokeFile == null ? "embedded-profile" : "embedded-smoke-profile");
        env = await CoreWebView2Environment.CreateAsync(null, profile);
        if (appUrl != null) {
            Uri uri;
            if (!Uri.TryCreate(appUrl, UriKind.Absolute, out uri) || uri.Scheme != "http" || uri.Host != "127.0.0.1") throw new ArgumentException("URL приложения должен быть локальным http://127.0.0.1");
            // Separate profile prevents portal pages sharing the app's browser storage.
            var appEnv = await CoreWebView2Environment.CreateAsync(null, profile + "-app");
            var app = new WebView2 { Dock = DockStyle.Fill }; split.Panel1.Controls.Add(app);
            await app.EnsureCoreWebView2Async(appEnv);
            app.CoreWebView2.WebMessageReceived += async (s,e) => {
                try {
                    if (!e.Source.StartsWith(uri.GetLeftPart(UriPartial.Authority) + "/", StringComparison.Ordinal)) return;
                    var msg = new System.Web.Script.Serialization.JavaScriptSerializer().Deserialize<System.Collections.Generic.Dictionary<string,object>>(e.WebMessageAsJson);
                    if ((string)msg["kind"] == "rpa-mode") {
                        rpaActive = msg.ContainsKey("active") && msg["active"] is bool && (bool)msg["active"];
                        if (!rpaActive) { split.Panel1Collapsed = false; split.Panel2Collapsed = true; stopRun = true; }
                    } else if ((string)msg["kind"] == "rpa-open" && rpaActive) {
                        split.Panel2Collapsed = false;
                        split.SplitterDistance = Math.Max(450, Width / 2);
                        if (tabs.TabCount == 0) await AddTab("about:blank");
                    }
                } catch(Exception ex) { output.Text = ex.Message; }
            };
            app.CoreWebView2.Navigate(appUrl);
        } else split.Panel1.Controls.Add(new Label { Dock = DockStyle.Fill, Text = "Экспериментальная оболочка.\nДля интерфейса OpsDigest передайте локальный URL приложения.\nПрофиль Chrome не используется." });
        split.Panel2Collapsed = true;
        if (smokeFile == null) return;
        split.Panel2Collapsed = false;
        var first = await AddTab("about:blank");
        await AddTab("about:blank");
        if (smokeFile != null) {
            var loaded = new System.Threading.Tasks.TaskCompletionSource<bool>();
            first.CoreWebView2.NavigationCompleted += (s,e) => loaded.TrySetResult(e.IsSuccess);
            first.CoreWebView2.NavigateToString("<html><body><button id='plain'>Plain</button><iframe srcdoc=\"<button>Nested</button>\"></iframe></body></html>");
            if (!await loaded.Task) throw new Exception("Navigation failed");
            string dom = await first.CoreWebView2.CallDevToolsProtocolMethodAsync("DOM.getDocument", "{\"depth\":-1,\"pierce\":true}");
            string text = await first.CoreWebView2.ExecuteScriptAsync("document.querySelector('#plain').textContent");
            if (!dom.Contains("Nested") || !text.Contains("Plain")) throw new Exception("DOM/iframe read failed");
            await first.CoreWebView2.ExecuteScriptAsync("document.querySelector('#plain').click(); document.querySelector('#plain').textContent='Done'");
            string after = await first.CoreWebView2.ExecuteScriptAsync("document.querySelector('#plain').textContent");
            if (!after.Contains("Done")) throw new Exception("Action failed");
            await first.CoreWebView2.ExecuteScriptAsync(picker);
            var captured = new System.Threading.Tasks.TaskCompletionSource<bool>();
            first.CoreWebView2.WebMessageReceived += (s,e) => captured.TrySetResult(true);
            await first.CoreWebView2.ExecuteScriptAsync("document.querySelector('#plain').dispatchEvent(new MouseEvent('click',{bubbles:true,altKey:true,cancelable:true}))");
            var winner = await System.Threading.Tasks.Task.WhenAny(captured.Task, System.Threading.Tasks.Task.Delay(5000));
            if (winner != captured.Task || selection == null || !selection.Contains("Done")) throw new Exception("Picker failed");
            selectionReady = new System.Threading.Tasks.TaskCompletionSource<bool>();
            await first.CoreWebView2.ExecuteScriptAsync("document.querySelector('iframe').contentDocument.querySelector('button').dispatchEvent(new MouseEvent('click',{bubbles:true,altKey:true,cancelable:true}))");
            winner = await System.Threading.Tasks.Task.WhenAny(selectionReady.Task, System.Threading.Tasks.Task.Delay(5000));
            if (winner != selectionReady.Task || !selection.Contains("Nested")) throw new Exception("Iframe picker failed");
            var target = serializer.Deserialize<System.Collections.Generic.Dictionary<string,object>>(selection);
            string replay = await ExecuteStep(first, new Step { action="read", value="", target=target });
            if(!replay.Contains("Nested")) throw new Exception("Iframe replay failed");
            LoadScenario(serializer.Serialize(new {version=1, steps=new [] { new Step {action="check",value="Nested",target=target} }}));
            if(steps.Count!=1) throw new Exception("Scenario roundtrip failed");
            await ExecuteStep(first, steps[0]);
            await first.CoreWebView2.ExecuteScriptAsync("document.querySelector('iframe').contentDocument.body.insertAdjacentHTML('beforeend','<button>Nested</button>')");
            bool ambiguous=false;
            try { await ExecuteStep(first, steps[0]); } catch { ambiguous=true; }
            if(!ambiguous) throw new Exception("Ambiguous target was accepted");
            bool rejected=false;
            try { LoadScenario("{\"version\":1,\"steps\":[{\"action\":\"javascript\"}]}"); } catch { rejected=true; }
            if(!rejected || steps.Count!=1) throw new Exception("Invalid scenario accepted");
            split.Panel1Collapsed = true;
            File.WriteAllText(smokeFile, "PASS: two embedded tabs; DOM iframe; picker; iframe replay; scenario roundtrip; invalid action rejected; expanded panel; runtime=" + env.BrowserVersionString);
            Close();
        }
    }
}
