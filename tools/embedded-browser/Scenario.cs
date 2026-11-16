using System;
using System.IO;
using System.Collections.Generic;
using System.Windows.Forms;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

partial class Prototype {
    class Step {
        public string action { get; set; }
        public string value { get; set; }
        public Dictionary<string,object> target { get; set; }
        public override string ToString() { return action + " · " + (target != null ? Convert.ToString(target["name"]) : value); }
    }
    readonly List<Step> steps = new List<Step>();
    readonly ListBox stepList = new ListBox { Dock = DockStyle.Fill };
    readonly ComboBox actionType = new ComboBox { DropDownStyle = ComboBoxStyle.DropDownList, Width = 130 };
    readonly string[] actionKeys = {"read","check","click","fill","navigate"};
    readonly TextBox stepValue = new TextBox { Width = 160 };
    readonly Dictionary<WebView2,List<CoreWebView2Frame>> childFrames = new Dictionary<WebView2,List<CoreWebView2Frame>>();
    bool running, stopRun;
    readonly JavaScriptSerializer serializer = new JavaScriptSerializer();
    void SetupScenario() {
        var editor = new Panel { Dock = DockStyle.Bottom, Height = 210 };
        var controls = new FlowLayoutPanel { Dock = DockStyle.Top, Height = 76, AutoScroll = true };
        actionType.Items.AddRange(new object[] { "Прочитать", "Проверить текст", "Нажать", "Ввести текст", "Перейти по URL" }); actionType.SelectedIndex = 0;
        actionType.AccessibleName = "Действие шага";
        stepValue.AccessibleName = "Значение шага или адрес перехода";
        stepList.AccessibleName = "Шаги сценария";
        controls.Controls.Add(new Label { Text = "Действие и значение:", AutoSize = true });
        controls.Controls.Add(actionType); controls.Controls.Add(stepValue);
        Action<string,Action> button = (label, handler) => {
            var b = new Button { Text = label, AutoSize = true }; controls.Controls.Add(b);
            b.Click += (s,e) => { try { if (running && label != "Стоп") throw new Exception("Сначала остановите выполнение"); handler(); } catch(Exception ex) { output.Text = ex.Message; } };
        };
        button("Добавить шаг", () => {
            string action = actionKeys[actionType.SelectedIndex];
            Dictionary<string,object> target = null;
            if (action != "navigate") {
                if (selection == null) throw new Exception("Выберите элемент через Alt + клик");
                target = serializer.Deserialize<Dictionary<string,object>>(selection);
            } else ValidateUrl(stepValue.Text);
            steps.Add(new Step { action = action, value = stepValue.Text, target = target }); RenderSteps();
        });
        button("Удалить", () => { if(stepList.SelectedIndex >= 0) { steps.RemoveAt(stepList.SelectedIndex); RenderSteps(); } });
        button("↑", () => MoveStep(-1)); button("↓", () => MoveStep(1));
        button("Сохранить сценарий", () => {
            using(var dialog = new SaveFileDialog { Filter = "Сценарий JSON|*.json", FileName = "scenario.json" })
                if(dialog.ShowDialog() == DialogResult.OK) File.WriteAllText(dialog.FileName, serializer.Serialize(new {version=1, steps=steps}));
        });
        button("Открыть сценарий", () => {
            using(var dialog = new OpenFileDialog { Filter = "Сценарий JSON|*.json" })
                if(dialog.ShowDialog() == DialogResult.OK) LoadScenario(File.ReadAllText(dialog.FileName));
        });
        button("Выполнить", async () => { try { await RunScenario(); } catch(Exception ex) { output.Text = ex.Message; running = false; } });
        button("Стоп", () => stopRun = true);
        editor.Controls.Add(stepList); editor.Controls.Add(controls);
        split.Panel2.Controls.Add(editor);
    }
    void MoveStep(int delta) {
        int i = stepList.SelectedIndex, j = i + delta;
        if (i < 0 || j < 0 || j >= steps.Count) return;
        var item = steps[i]; steps[i] = steps[j]; steps[j] = item; RenderSteps(); stepList.SelectedIndex = j;
    }
    void RenderSteps() { stepList.Items.Clear(); foreach(var step in steps) stepList.Items.Add((stepList.Items.Count+1) + ". " + step); }
    void ValidateUrl(string value) {
        Uri uri; if(!Uri.TryCreate(value, UriKind.Absolute, out uri) || (uri.Scheme != "https" && uri.Scheme != "http")) throw new Exception("Переход требует адрес http:// или https://");
    }
    void LoadScenario(string text) {
        if(text.Length > 1000000) throw new Exception("Сценарий слишком большой");
        var doc = serializer.Deserialize<Dictionary<string,object>>(text);
        if(Convert.ToInt32(doc["version"]) != 1) throw new Exception("Неизвестная версия сценария");
        var parsed = serializer.Deserialize<List<Step>>(serializer.Serialize(doc["steps"]));
        if(parsed == null || parsed.Count > 200) throw new Exception("Максимум 200 шагов");
        foreach(var step in parsed) {
            if(!new List<string>{"read","check","click","fill","navigate"}.Contains(step.action)) throw new Exception("Неизвестное действие");
            if(step.action == "navigate") ValidateUrl(step.value);
            else if(step.target == null || !step.target.ContainsKey("tag") || !step.target.ContainsKey("page") || !step.target.ContainsKey("name")) throw new Exception("Некорректная цель шага");
        }
        steps.Clear(); steps.AddRange(parsed); RenderSteps();
    }
    async Task RunScenario() {
        if(running || steps.Count == 0 || tabs.TabCount == 0) return;
        if(MessageBox.Show("Выполнить сценарий в текущей вкладке? Клики и ввод могут изменить данные портала. Проверьте шаги перед запуском.", "Пробный запуск", MessageBoxButtons.OKCancel) != DialogResult.OK) return;
        running = true; stopRun = false;
        var view = Current();
        try {
            for(int i=0; i<steps.Count; i++) {
                if(stopRun) { output.Text = "Остановлено перед шагом " + (i+1); return; }
                stepList.SelectedIndex = i;
                var step = steps[i];
                if(step.action == "navigate") {
                    var loaded = new TaskCompletionSource<bool>();
                    EventHandler<CoreWebView2NavigationCompletedEventArgs> handler = (s,e) => loaded.TrySetResult(e.IsSuccess);
                    view.CoreWebView2.NavigationCompleted += handler;
                    try {
                        view.CoreWebView2.Navigate(step.value);
                        if(await Task.WhenAny(loaded.Task, Task.Delay(20000)) != loaded.Task || !await loaded.Task) throw new Exception("Переход не завершён");
                    } finally { view.CoreWebView2.NavigationCompleted -= handler; }
                } else output.Text = await ExecuteStep(view, step);
            }
            output.Text += "\r\nСценарий завершён";
        } catch(Exception ex) { output.Text = "Остановлено: " + ex.Message; }
        finally { running = false; }
    }
    async Task<string> ExecuteStep(WebView2 view, Step step) {
        string spec = serializer.Serialize(step);
        string finder = @"(s,act)=>{if(location.origin+location.pathname!==s.target.page)return {count:0};
const name=e=>(e.getAttribute('aria-label')||e.labels?.[0]?.innerText||e.getAttribute('placeholder')||e.innerText||'').trim().slice(0,160);
const es=Array.from(document.querySelectorAll('*')).filter(e=>e.tagName.toLowerCase()===s.target.tag && e.getClientRects().length && getComputedStyle(e).visibility!=='hidden' && !e.matches('input[type=password],input[type=hidden]') && name(e)===s.target.name);
if(!act||es.length!==1)return {count:es.length};const e=es[0];
if(s.action==='click')e.click();
if(s.action==='fill'){if(!e.matches('input,textarea'))throw Error('Выберите поле ввода');const p=e.tagName==='INPUT'?HTMLInputElement.prototype:HTMLTextAreaElement.prototype;Object.getOwnPropertyDescriptor(p,'value').set.call(e,s.value);e.dispatchEvent(new Event('input',{bubbles:true}));e.dispatchEvent(new Event('change',{bubbles:true}));}
const text=e.matches('input,textarea,select')?e.value:e.innerText;
if(s.action==='check'&&!text.includes(s.value))throw Error('Текст не совпал');return {count:1,text:(text||'').slice(0,2000)};}";
        var executors = new List<Func<string,Task<string>>>();
        executors.Add(js => view.CoreWebView2.ExecuteScriptAsync(js));
        if(childFrames.ContainsKey(view)) foreach(var frame in childFrames[view]) {
            if(frame.IsDestroyed() == 0) { var local = frame; executors.Add(js=>local.ExecuteScriptAsync(js)); }
        }
        Func<string,bool,string> script = (json,act) => "(" + finder + ")(" + json + "," + (act?"true":"false") + ")";
        int total=0; Func<string,Task<string>> chosen=null;
        foreach(var execute in executors) {
            string result = await execute(script(spec,false));
            var data = serializer.Deserialize<Dictionary<string,object>>(result);
            if(data == null) throw new Exception("Фрейм недоступен");
            int count = Convert.ToInt32(data["count"]); total+=count; if(count==1)chosen=execute;
        }
        if(total!=1 || chosen==null) throw new Exception("Найдено элементов: " + total + ". Уточните цель, действие не выполнено.");
        string final = await chosen(script(spec,true));
        var check = serializer.Deserialize<Dictionary<string,object>>(final);
        if(check == null || Convert.ToInt32(check["count"])!=1) throw new Exception("Страница изменилась перед действием");
        return step.action + ": " + Convert.ToString(check["text"]);
    }
}
