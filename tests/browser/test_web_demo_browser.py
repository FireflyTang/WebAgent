from __future__ import annotations

import http.client
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, sync_playwright


def _non_loopback_address() -> str:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
    finally:
        probe.close()
    if address.startswith("127."):
        pytest.skip("没有可用的非 loopback IPv4 地址")
    return address


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("0.0.0.0", 0))
        return listener.getsockname()[1]


def _wait_for_server(host: str, port: int) -> None:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            connection = http.client.HTTPConnection(host, port, timeout=0.5)
            connection.request("GET", "/healthz")
            if connection.getresponse().status == 200:
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("Uvicorn 未在规定时间内启动")


@pytest.fixture
def web_server(tmp_path: Path):
    host, port, provider_port = _non_loopback_address(), _free_port(), _free_port()

    class ProviderHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path != "/v1/models":
                self.send_error(404)
                return
            payload = json.dumps(
                {
                    "data": [
                        {"id": "claude-code-agent"},
                        {"id": "browser-fake-model"},
                        {"id": "keyboard-model"},
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, _format: str, *args: object) -> None:
            del args

    provider_server = ThreadingHTTPServer(("0.0.0.0", provider_port), ProviderHandler)
    provider_thread = threading.Thread(target=provider_server.serve_forever, daemon=True)
    provider_thread.start()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(Path.cwd() / "src"),
            "HOST": "0.0.0.0",
            "PORT": str(port),
            "API_KEY": "browser-demo-key",
            "RUNTIME_BACKEND": "fake",
            "SANDBOX_BACKEND": "local",
            "DATABASE_URL": f"sqlite:///{tmp_path / 'browser.db'}",
            "WORKSPACE_ROOT": str(tmp_path / "workspaces"),
            "FAKE_STREAM_DELAY_MS": "0",
            "FAKE_LONG_TASK_DELAY_MS": "0",
            "CLAUDE_AVAILABLE_MODELS": "claude-code-agent,browser-fake-model,keyboard-model",
        }
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", str(port)],
        cwd=Path.cwd(),
        env=environment,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_for_server(host, port)
        yield {"web": f"http://{host}:{port}", "provider": f"http://127.0.0.1:{provider_port}"}
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        provider_server.shutdown()
        provider_server.server_close()
        provider_thread.join(timeout=5)


@pytest.fixture
def browser_page(monkeypatch: pytest.MonkeyPatch) -> Page:
    for name in (
        "http_proxy",
        "https_proxy",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "all_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    with sync_playwright() as playwright:
        browser: Browser = playwright.chromium.launch(args=["--no-proxy-server"])
        context = browser.new_context(viewport={"width": 1440, "height": 900})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            yield page
            assert errors == []
        finally:
            context.close()
            browser.close()


def _click_visible_new_session(page: Page) -> None:
    desktop_button = page.locator("#newSession")
    if desktop_button.is_visible():
        desktop_button.click()
        return

    mobile_button = page.locator(".mobile-rail").get_by_role("button", name="新建会话", exact=True)
    assert desktop_button.is_hidden()
    assert mobile_button.is_visible()
    mobile_button.click()


def _connect_and_create(page: Page, web_server: dict[str, str]) -> None:
    page.goto(web_server["web"], wait_until="domcontentloaded", timeout=10_000)
    page.locator("#providerEndpoint").fill(web_server["provider"])
    page.locator("#providerApiKey").fill("provider-test-key")
    page.locator("#testProvider").click()
    page.locator(".provider-tested").wait_for(state="visible", timeout=10_000)
    page.locator("#saveProvider").click()
    page.locator(".connection-popover").wait_for(state="hidden", timeout=10_000)
    _click_visible_new_session(page)
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)


def test_connect_uses_visible_mobile_new_session_entrypoint(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.set_viewport_size({"width": 820, "height": 620})

    _connect_and_create(page, web_server)

    assert page.locator("#newSession").is_hidden()
    assert (
        page.locator(".mobile-rail").get_by_role("button", name="新建会话", exact=True).is_visible()
    )
    assert page.evaluate("localStorage.getItem('oca.active-session')")


def test_real_session_model_files_chat_log_and_reload(
    web_server: dict[str, str], browser_page: Page, tmp_path: Path
) -> None:
    page = browser_page
    _connect_and_create(page, web_server)
    assert page.locator(".topbar h1").inner_text() == "新会话"
    assert page.locator(".topbar p").count() == 0
    assert page.locator(".files-panel").get_by_text("/workspace", exact=True).count() == 0

    trigger = page.locator(".model-trigger")
    trigger.focus()
    trigger.press("ArrowDown")
    assert page.get_by_role("listbox", name="选择模型").is_visible()
    trigger.press("Enter")
    page.wait_for_timeout(300)
    chosen = trigger.inner_text().removeprefix("模型：").strip()
    assert chosen in {"browser-fake-model", "keyboard-model"}

    source = tmp_path / "single.txt"
    source.write_text("来自浏览器的文件\n", encoding="utf-8")
    page.locator("#fileInput").set_input_files(source)
    row = page.get_by_role("treeitem", name="single.txt")
    row.wait_for(timeout=10_000)
    row.click()
    assert "selected" not in (row.get_attribute("class") or "")
    with page.expect_download(timeout=10_000) as download_info:
        row.dblclick()
    download = download_info.value
    assert download.suggested_filename == "single.txt"
    assert Path(download.path()).read_text(encoding="utf-8") == "来自浏览器的文件\n"

    active_html = tmp_path / "danger.html"
    active_html.write_text("<script>window.opener.__htmlExecuted=true</script>", encoding="utf-8")
    page.locator("#fileInput").set_input_files(active_html)
    html_row = page.get_by_role("treeitem", name="danger.html")
    html_row.wait_for(timeout=10_000)
    with page.expect_download(timeout=10_000) as html_download_info:
        html_row.dblclick()
    assert html_download_info.value.suggested_filename == "danger.html"
    assert page.evaluate("window.__htmlExecuted") is None

    project = tmp_path / "project"
    (project / "src").mkdir(parents=True)
    (project / "README.md").write_text("目录上传\n", encoding="utf-8")
    (project / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    page.locator("#directoryInput").set_input_files(project)
    project_row = page.get_by_role("treeitem", name="project")
    main_row = page.get_by_role("treeitem", name="main.py")
    project_row.wait_for(timeout=10_000)
    main_row.wait_for(timeout=10_000)
    assert page.locator(".files-panel").get_by_text("/workspace", exact=True).count() == 0
    project_row.click()
    assert project_row.get_attribute("aria-expanded") == "false"
    assert main_row.count() == 0
    file_search = page.get_by_role("textbox", name="搜索文件和目录")
    file_search.fill("main.py")
    assert project_row.is_visible()
    assert page.get_by_role("treeitem", name="src").is_visible()
    assert main_row.is_visible()
    assert project_row.get_attribute("aria-expanded") == "true"
    file_search.fill("project")
    assert page.get_by_role("treeitem", name="README.md").is_visible()
    assert main_row.is_visible()
    file_search.fill("")
    assert project_row.get_attribute("aria-expanded") == "false"
    assert main_row.count() == 0
    project_row.click()
    main_row.wait_for(timeout=5_000)
    page.set_viewport_size({"width": 820, "height": 620})
    page.get_by_role("button", name="打开沙箱文件").click()
    drawer = page.locator(".file-drawer")
    assert drawer.get_by_text("/workspace", exact=True).count() == 0
    assert drawer.get_by_role("treeitem", name="project").is_visible()
    assert page.locator("[id=fileInput]").count() == 1
    assert page.locator("[id=drawerfileInput]").count() == 1
    page.get_by_role("button", name="关闭文件面板").click()
    page.set_viewport_size({"width": 1440, "height": 900})

    page.locator("#prompt").fill("创建一个计算器，实现加法并运行测试")
    page.locator("#prompt").press("Enter")
    page.get_by_text("测试通过", exact=False).last.wait_for(timeout=15_000)
    page.get_by_role("button", name="发送").wait_for(timeout=10_000)
    page.locator(".topbar h1").get_by_text(
        "创建一个计算器，实现加法并运行测试", exact=True
    ).wait_for(timeout=5_000)
    assert page.locator("#prompt").is_enabled()
    assert page.locator(".step-row").count() > 0
    assert page.locator(".task-card").inner_text().count(":") > 0

    # FakeRuntime deliberately emits no provider diagnostics. Seed one durable
    # diagnostic entry so the browser contract exercises both the readable raw
    # turn transcript and a detailed diagnostic event in the same iframe.
    session_id = page.evaluate("localStorage.getItem('oca.active-session')")
    with sqlite3.connect(tmp_path / "browser.db") as database:
        database.execute(
            """
            INSERT INTO session_log_entries
                (session_id, created_at, title, content, metadata_json, event_type)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                session_id,
                "2026-08-11T00:00:00+00:00",
                "运行时诊断：browser.smoke",
                "浏览器诊断事件已记录",
                '{"事件类型":"browser.smoke"}',
                "browser.smoke",
            ),
        )

    page.locator("#openLog").click()
    page.locator("#logDialog").wait_for(state="visible")
    assert "详细诊断" in page.locator("#logDialog").inner_text()
    assert session_id in page.locator("#logDialog h2").inner_text()
    assert page.locator("#sessionLogFrame").get_attribute("sandbox") == ""
    assert page.locator("#logDialog .dialog-note").inner_text() == (
        "按实际执行顺序展示用户输入、模型输出、工具参数与结果。"
    )
    log_body = page.locator("#sessionLogFrame").content_frame.locator("body")
    log_body.wait_for(state="visible")
    log_text = log_body.inner_text()
    assert "运行时诊断：browser.smoke" in log_text
    assert "用户消息" in log_text
    assert "创建一个计算器，实现加法并运行测试" in log_text
    assert "Claude Code 输出" in log_text
    page.keyboard.press("Escape")
    assert page.locator("#logDialog").is_hidden()

    page.reload(wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)
    page.get_by_text("测试通过", exact=False).last.wait_for(timeout=10_000)
    assert chosen in page.locator(".model-trigger").inner_text()
    assert page.locator(".topbar h1").inner_text() == "创建一个计算器，实现加法并运行测试"
    page.locator("#newSession").click()
    page.locator(".topbar h1").get_by_text("新会话", exact=True).wait_for(timeout=5_000)
    page.locator(".session-row").filter(has_text="创建一个计算器，实现加法并运行测试").click()
    assert page.locator(".topbar h1").inner_text() == "创建一个计算器，实现加法并运行测试"


def test_late_log_response_cannot_open_for_another_session(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        const nativeFetch=window.fetch.bind(window);let logCalls=0;
        window.fetch=(input,options)=>{
          const url=String(input);
          if(!url.includes('/log'))return nativeFetch(input,options);
          logCalls+=1;
          if(logCalls===1)return new Promise(resolve=>{window.__resolveLateLog=()=>resolve(new Response('<html><body>late A log</body></html>',{status:200,headers:{'Content-Type':'text/html'}}))});
          return Promise.resolve(new Response('<html><body>B log</body></html>',{status:200,headers:{'Content-Type':'text/html'}}));
        };
        """
    )
    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#newSession").click()
    page.wait_for_function(
        "previous => localStorage.getItem('oca.active-session') !== previous", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.locator("#openLog").click()
    page.locator(f'.session-row[data-session-id="{session_b}"]').click()
    page.evaluate("window.__resolveLateLog()")
    page.wait_for_timeout(100)
    assert page.locator("#logDialog").count() == 0

    page.locator("#openLog").click()
    page.locator("#logDialog").wait_for(state="visible", timeout=5_000)
    assert session_b in page.locator("#logDialog h2").inner_text()
    assert session_a not in page.locator("#logDialog h2").inner_text()


def test_file_tree_fold_state_is_isolated_between_sessions_with_the_same_paths(
    web_server: dict[str, str], browser_page: Page, tmp_path: Path
) -> None:
    page = browser_page
    shared_tree = tmp_path / "same-tree"
    shared_tree.mkdir()
    (shared_tree / "child.txt").write_text("session-local tree\n", encoding="utf-8")

    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#directoryInput").set_input_files(shared_tree)
    folder = page.get_by_role("treeitem", name="same-tree")
    child = page.get_by_role("treeitem", name="child.txt")
    child.wait_for(timeout=10_000)
    folder.click()
    assert folder.get_attribute("aria-expanded") == "false"
    assert child.count() == 0
    page.get_by_label("搜索文件和目录").fill("child")
    child.wait_for(timeout=5_000)

    page.locator("#newSession").click()
    page.wait_for_function(
        "previous => localStorage.getItem('oca.active-session') !== previous", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    assert page.get_by_label("搜索文件和目录").input_value() == ""
    page.locator("#directoryInput").set_input_files(shared_tree)
    child.wait_for(timeout=10_000)
    assert folder.get_attribute("aria-expanded") == "true"

    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.wait_for_function(
        "expected => localStorage.getItem('oca.active-session') === expected", arg=session_a
    )
    assert page.get_by_label("搜索文件和目录").input_value() == "child"
    child.wait_for(timeout=10_000)
    page.get_by_label("搜索文件和目录").fill("")
    folder.wait_for(timeout=10_000)
    assert folder.get_attribute("aria-expanded") == "false"
    assert child.count() == 0

    page.locator(f'.session-row[data-session-id="{session_b}"]').click()
    page.wait_for_function(
        "expected => localStorage.getItem('oca.active-session') === expected", arg=session_b
    )
    child.wait_for(timeout=10_000)
    assert folder.get_attribute("aria-expanded") == "true"

    page.set_viewport_size({"width": 820, "height": 620})
    page.get_by_role("button", name="打开 Session 列表").click()
    mobile_list = page.get_by_role("navigation", name="移动 Session 列表")
    assert mobile_list.is_visible()
    mobile_list.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.wait_for_function(
        "expected => localStorage.getItem('oca.active-session') === expected", arg=session_a
    )
    assert mobile_list.is_hidden()
    page.get_by_role("button", name="打开沙箱文件").click()
    assert page.get_by_role("button", name="关闭文件面板").is_visible()
    page.keyboard.press("Escape")


def test_sidebar_resize_collapse_persistence_mobile_drawer_and_low_height(
    web_server: dict[str, str], browser_page: Page, tmp_path: Path
) -> None:
    page = browser_page
    _connect_and_create(page, web_server)
    metrics_dir = tmp_path / "metrics"
    metrics_dir.mkdir()
    (metrics_dir / "child.txt").write_text("metrics\n", encoding="utf-8")
    page.locator("#directoryInput").set_input_files(metrics_dir)
    metrics_row = page.get_by_role("treeitem", name="metrics")
    metrics_row.wait_for(timeout=10_000)
    assert page.get_by_role("treeitem", name="/workspace").count() == 0
    file_row_metrics = metrics_row.evaluate(
        "node => ({fontSize: parseFloat(getComputedStyle(node).fontSize), height: node.getBoundingClientRect().height, sizeFont: parseFloat(getComputedStyle(node.querySelector('.file-size')).fontSize), iconWidth: node.querySelector('.file-icon svg').getBoundingClientRect().width, caretWidth: node.querySelector('.tree-caret svg').getBoundingClientRect().width})"
    )
    assert file_row_metrics["fontSize"] >= 12
    assert file_row_metrics["sizeFont"] >= 10
    assert file_row_metrics["height"] >= 36
    assert file_row_metrics["iconWidth"] >= 16
    assert 10 <= file_row_metrics["caretWidth"] <= 12
    workspace_box = page.locator(".workspace").bounding_box()
    assert page.locator(".topbar h1").bounding_box()["x"] >= workspace_box["x"] + 27
    assert page.locator(".conversation-inner").bounding_box()["x"] >= workspace_box["x"] + 9
    assert 220 <= page.locator(".session-rail").bounding_box()["width"] <= 240
    assert 280 <= page.locator(".files-panel").bounding_box()["width"] <= 310
    left_before = page.locator(".session-rail").bounding_box()["width"]
    right_before = page.locator(".files-panel").bounding_box()["width"]
    left_separator = page.get_by_role("separator", name="调整 Session 栏宽度").bounding_box()
    page.mouse.move(left_separator["x"] + left_separator["width"] / 2, left_separator["y"] + 100)
    page.mouse.down()
    page.mouse.move(
        left_separator["x"] + left_separator["width"] / 2 + 44, left_separator["y"] + 100
    )
    page.mouse.up()
    right_separator = page.get_by_role("separator", name="调整沙箱文件栏宽度").bounding_box()
    page.mouse.move(right_separator["x"] + right_separator["width"] / 2, right_separator["y"] + 100)
    page.mouse.down()
    page.mouse.move(
        right_separator["x"] + right_separator["width"] / 2 - 52, right_separator["y"] + 100
    )
    page.mouse.up()
    left_dragged = page.locator(".session-rail").bounding_box()["width"]
    right_dragged = page.locator(".files-panel").bounding_box()["width"]
    assert left_dragged >= left_before + 40
    assert right_dragged >= right_before + 48
    page.reload(wait_until="domcontentloaded")
    assert abs(page.locator(".session-rail").bounding_box()["width"] - left_dragged) < 1
    assert abs(page.locator(".files-panel").bounding_box()["width"] - right_dragged) < 1
    page.get_by_role("button", name="收起 Session 栏").click()
    page.get_by_role("button", name="收起沙箱文件").click()
    page.reload(wait_until="domcontentloaded")
    assert page.get_by_role("button", name="展开 Session 栏").is_visible()
    assert page.get_by_role("button", name="展开沙箱文件").is_visible()

    page.set_viewport_size({"width": 820, "height": 620})
    page.get_by_role("button", name="打开沙箱文件").click()
    assert page.get_by_role("button", name="关闭文件面板").is_visible()
    assert page.locator(".file-drawer").bounding_box()["width"] <= 290
    page.keyboard.press("Escape")
    assert page.get_by_role("button", name="关闭文件面板").is_hidden()
    composer = page.locator("#composer").bounding_box()
    assert composer and composer["y"] + composer["height"] <= 620
    assert page.evaluate(
        "document.documentElement.scrollWidth === document.documentElement.clientWidth"
    )


def test_progress_steps_are_visible_before_delta_independent_and_stopped(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class ControlledSocket {
          constructor() { this.readyState = 0; setTimeout(() => { this.readyState=1; this.onopen?.({}); }, 0); }
          send(raw) {
            const m=JSON.parse(raw), emit=x=>this.onmessage?.({data:JSON.stringify(x)});
            if(m.type==='hello') { this.sid=m.session_id; return setTimeout(()=>emit({type:'ready',session_id:m.session_id,task_state:'idle'}),0); }
            if(m.type==='message') {
              this.turn='turn-1';
              const old=new Date(Date.now()-168000).toISOString();
              emit({type:'turn_started',session_id:this.sid,turn_id:this.turn,sequence:1,at:new Date().toISOString()});
              emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:2,at:old,phase:'prepare',message:'准备环境',status:'started',tool_use_id:'prepare-1'});
              emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:3,at:new Date().toISOString(),phase:'prepare',message:'准备环境',status:'completed',tool_use_id:'prepare-1'});
              emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:4,at:new Date().toISOString(),phase:'read',message:'读取项目文件',status:'started',tool_use_id:'read-1'});
              emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:5,at:new Date().toISOString(),phase:'test',message:'运行测试',status:'running',tool_use_id:'test-1'});
            }
            if(m.type==='stop') emit({type:'done',session_id:this.sid,turn_id:this.turn,sequence:6,at:new Date().toISOString(),completed:false,stop_reason:'stopped',usage:null});
          }
          close(){this.readyState=3;}
        }
        window.WebSocket=ControlledSocket;
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("运行一个长任务")
    page.locator("#prompt").press("Enter")
    page.get_by_text("读取项目文件", exact=True).wait_for(timeout=5_000)
    assert page.locator(".message.assistant.working-message").is_visible()
    assert page.locator('.step-row[data-status="running"]').count() == 2
    assert "02:48" in page.locator(".task-card").inner_text()
    activities = page.locator(".step-row button")
    assert activities.count() == 3
    activities.nth(0).click()
    assert page.locator(".activities").count() == 1
    activities.nth(1).click()
    assert page.locator(".activities").count() == 2
    activities.nth(0).click()
    assert page.locator(".activities").count() == 1
    page.locator(".session-row.selected").click(button="right")
    delete_item = page.get_by_role("menuitem", name="删除会话")
    assert delete_item.is_visible()
    assert delete_item.is_disabled()
    assert page.get_by_text("请先停止当前任务", exact=True).is_visible()
    page.keyboard.press("Escape")
    assert delete_item.is_hidden()
    page.get_by_role("button", name="停止").last.click()
    page.get_by_text("已停止", exact=False).first.wait_for(timeout=5_000)
    assert page.locator('.step-row[data-status="stopped"]').count() == 2
    page.locator("#prompt").fill("下一轮")
    assert page.locator("#sendButton").is_enabled()


@pytest.mark.parametrize(
    ("lifecycle_state", "banner_text"),
    [("paused", "沙箱已暂停"), ("expiring", "沙箱即将删除")],
)
def test_starting_complete_refreshes_lifecycle_without_stealing_background_selection(
    web_server: dict[str, str], browser_page: Page, lifecycle_state: str, banner_text: str
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__lifecycleSockets=new Map();
        class LifecycleSocket {
          constructor(){this.readyState=0;this.sequence=1;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;window.__lifecycleSockets.set(this.sid,this);
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0);
            }
            if(value.type==='message'){
              this.turn=`lifecycle-${this.sid}`;window.__lifecycleRunning=this;
              this.emit({type:'turn_started',session_id:this.sid,turn_id:this.turn,sequence:this.sequence++,at:new Date().toISOString()});
            }
          }
          startingComplete(){this.emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:this.sequence++,at:new Date().toISOString(),phase:'starting',status:'completed',message:'沙箱准备完成',tool_use_id:'sandbox-start'})}
          close(){this.readyState=3}
        }
        window.WebSocket=LifecycleSocket;
        window.__emitStartingComplete=()=>window.__lifecycleRunning.startingComplete();
        """
    )
    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    control = {"resumed": False, "gets": 0}

    def route_sessions(route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        data = response.json()
        control["gets"] += 1
        for session in data.get("sessions", []):
            if session.get("session_id") == session_a:
                session["state"] = "active" if control["resumed"] else lifecycle_state
                session["delete_at"] = None if control["resumed"] else "2026-08-11T12:00:00.000Z"
        route.fulfill(
            status=response.status, content_type="application/json", body=json.dumps(data)
        )

    page.route("**/v1/sessions", route_sessions)
    page.reload(wait_until="domcontentloaded")
    page.get_by_text(banner_text, exact=False).wait_for(timeout=10_000)
    page.locator("#prompt").fill("恢复暂停沙箱")
    page.locator("#prompt").press("Enter")
    page.get_by_text("正在工作", exact=False).first.wait_for(timeout=5_000)
    control["resumed"] = True
    page.evaluate("window.__emitStartingComplete()")
    page.get_by_text(banner_text, exact=False).wait_for(state="hidden", timeout=5_000)
    assert page.evaluate("localStorage.getItem('oca.active-session')") == session_a

    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#prompt").fill("B 会话未发送草稿")
    gets_before = control["gets"]
    page.evaluate("window.__emitStartingComplete()")
    deadline = time.monotonic() + 5
    while control["gets"] == gets_before and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert control["gets"] > gets_before
    assert page.evaluate("localStorage.getItem('oca.active-session')") == session_b
    assert page.locator("#prompt").input_value() == "B 会话未发送草稿"


def test_disconnect_keeps_running_state_replays_and_can_stop_after_reconnect(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class ReconnectingSocket {
          static count=0;
          constructor(){this.instance=++ReconnectingSocket.count;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          send(raw){
            const m=JSON.parse(raw),emit=x=>this.onmessage?.({data:JSON.stringify(x)});
            if(m.type==='hello'){
              this.sid=m.session_id;
              return setTimeout(()=>{
                emit({type:'sync_begin',session_id:this.sid});
                if(this.instance>1){
                  emit({type:'user_message',session_id:this.sid,turn_id:'reconnect-turn',sequence:0,at:'2026-08-11T02:00:00.000Z',content:'启动后模拟断线'});
                  emit({type:'turn_started',session_id:this.sid,turn_id:'reconnect-turn',sequence:1,at:'2026-08-11T02:00:00.100Z'});
                  emit({type:'progress',session_id:this.sid,turn_id:'reconnect-turn',sequence:2,at:'2026-08-11T02:00:00.200Z',phase:'tool',message:'执行长任务',status:'running',tool_use_id:'long-1'});
                  emit({type:'ready',session_id:this.sid,task_state:'running',turn_id:'reconnect-turn',last_sequence:2});
                }else emit({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0});
              },0);
            }
            if(m.type==='message'&&this.instance===1){
              this.turn='reconnect-turn';
              emit({type:'turn_started',session_id:this.sid,turn_id:this.turn,sequence:1,at:new Date().toISOString()});
              emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:2,at:new Date().toISOString(),phase:'command',message:'执行长任务',status:'running',tool_use_id:'long-1'});
              setTimeout(()=>{this.readyState=3;this.onclose?.({code:1006})},80);
            }
            if(m.type==='stop'){
              window.__reconnectedStopTurn=m.turn_id;
              emit({type:'done',session_id:this.sid,turn_id:m.turn_id,sequence:3,at:new Date().toISOString(),completed:false,stop_reason:'stopped',usage:null});
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=ReconnectingSocket;
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("启动后模拟断线")
    page.locator("#prompt").press("Enter")
    page.get_by_text("执行长任务", exact=True).wait_for(timeout=5_000)
    page.locator("#connectionBadge b").get_by_text("重新连接", exact=True).wait_for(timeout=5_000)
    assert "正在工作" in page.locator(".task-card").inner_text()
    assert "正在停止" not in page.locator(".task-card").inner_text()
    assert page.locator("#sendButton").is_disabled()
    assert page.locator(".task-card .stop-inline").is_disabled()
    page.get_by_text("连接恢复后即可停止任务", exact=True).first.wait_for(timeout=5_000)
    assert page.evaluate("window.__reconnectedStopTurn") is None
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    assert page.locator("#sendButton").get_by_text("停止", exact=True).is_visible()
    assert page.locator("#sendButton").is_enabled()
    page.get_by_role("button", name="停止").last.click()
    page.get_by_text("已停止", exact=False).first.wait_for(timeout=5_000)
    assert page.evaluate("window.__reconnectedStopTurn") == "reconnect-turn"
    page.locator("#prompt").fill("重连停止后继续")
    assert page.locator("#sendButton").is_enabled()


def test_done_immediately_clears_session_summary_when_refresh_fails(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class DoneSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){this.sid=value.session_id;return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0}),0)}
            if(value.type==='message'){
              this.turn='done-refresh-turn';window.__doneSocket=this;
              this.emit({type:'turn_started',session_id:this.sid,turn_id:this.turn,sequence:1,at:new Date().toISOString()});
              this.emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:2,at:new Date().toISOString(),phase:'tool',message:'即将完成',status:'running',tool_use_id:'done-tool'});
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=DoneSocket;
        window.__emitDone=()=>window.__doneSocket.emit({type:'done',session_id:window.__doneSocket.sid,turn_id:window.__doneSocket.turn,sequence:9,at:new Date().toISOString(),completed:true,stop_reason:'stop',usage:null});
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("完成后刷新失败")
    page.locator("#prompt").press("Enter")
    page.get_by_text("即将完成", exact=True).wait_for(timeout=5_000)

    def fail_session_refresh(route) -> None:
        if route.request.method == "GET":
            route.fulfill(
                status=503, content_type="application/json", body='{"detail":"refresh failed"}'
            )
        else:
            route.continue_()

    page.route("**/v1/sessions", fail_session_refresh)
    page.evaluate("window.__emitDone()")
    page.get_by_text("任务完成", exact=False).first.wait_for(timeout=5_000)
    page.locator(".session-row.selected").get_by_text("待命", exact=True).wait_for(timeout=5_000)
    assert page.locator(".stop-inline").count() == 0
    page.locator("#prompt").fill("刷新失败也可继续")
    assert page.locator("#sendButton").is_enabled()


def test_terminal_done_beats_a_successful_stale_finishing_refresh(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class TerminalFirstSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0}),0);
            }
            if(value.type==='message'){
              this.turn='terminal-first-turn';window.__terminalFirstSocket=this;
              this.emit({type:'turn_started',session_id:this.sid,turn_id:this.turn,sequence:1,at:new Date().toISOString()});
              this.emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:2,at:new Date().toISOString(),phase:'tool',status:'running',message:'等待终态',tool_use_id:'terminal-tool'});
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=TerminalFirstSocket;
        window.__emitTerminalFirstDone=()=>window.__terminalFirstSocket.emit({type:'done',session_id:window.__terminalFirstSocket.sid,turn_id:window.__terminalFirstSocket.turn,sequence:4,at:new Date().toISOString(),completed:true,stop_reason:'stop',usage:null});
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("终态优先")
    page.locator("#prompt").press("Enter")
    page.get_by_text("等待终态", exact=True).wait_for(timeout=5_000)

    def stale_finishing_refresh(route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        data = response.json()
        for session in data.get("sessions", []):
            session["task_state"] = "finishing"
            session["active_turn_id"] = "terminal-first-turn"
            session["last_turn_sequence"] = 3
        route.fulfill(
            status=response.status, content_type="application/json", body=json.dumps(data)
        )

    page.route("**/v1/sessions", stale_finishing_refresh)
    page.evaluate("window.__emitTerminalFirstDone()")
    page.get_by_text("任务完成", exact=False).first.wait_for(timeout=5_000)
    page.locator(".session-row.selected").get_by_text("待命", exact=True).wait_for(timeout=5_000)
    page.locator("#prompt").fill("旧快照不能禁用下一轮")
    assert page.locator("#sendButton").is_enabled()


def test_replay_done_beats_ready_finishing_and_next_turn_needs_no_idle_cleanup(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class TerminalReplaySocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;
              return setTimeout(()=>{
                if(localStorage.getItem('__terminalReplay')==='1'){
                  const at=new Date().toISOString(),turn='replayed-terminal-turn';
                  this.emit({type:'sync_begin',session_id:this.sid});
                  this.emit({type:'user_message',session_id:this.sid,turn_id:turn,sequence:0,at,content:'重放终态'});
                  this.emit({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at});
                  this.emit({type:'delta',session_id:this.sid,turn_id:turn,sequence:2,at,content:'重放已经完成'});
                  this.emit({type:'done',session_id:this.sid,turn_id:turn,sequence:4,at,completed:true,stop_reason:'stop',usage:null});
                  this.emit({type:'ready',session_id:this.sid,task_state:'finishing',turn_id:turn,last_sequence:3});
                }else this.emit({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0});
              },0);
            }
            if(value.type==='message'){
              const at=new Date().toISOString(),turn='next-without-idle';
              this.emit({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at});
              this.emit({type:'delta',session_id:this.sid,turn_id:turn,sequence:2,at,content:'下一轮也完成'});
              this.emit({type:'done',session_id:this.sid,turn_id:turn,sequence:3,at,completed:true,stop_reason:'stop',usage:null});
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=TerminalReplaySocket;
        """
    )
    _connect_and_create(page, web_server)
    page.evaluate("localStorage.setItem('__terminalReplay','1')")
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("重放已经完成", exact=True).wait_for(timeout=5_000)
    page.get_by_text("任务完成", exact=False).first.wait_for(timeout=5_000)
    page.locator(".session-row.selected").get_by_text("待命", exact=True).wait_for(timeout=5_000)
    page.locator("#prompt").fill("没有 idle cleanup 也继续")
    assert page.locator("#sendButton").is_enabled()
    page.locator("#prompt").press("Enter")
    page.get_by_text("下一轮也完成", exact=True).wait_for(timeout=5_000)
    assert page.locator(".message.user").count() == 2


def test_send_throw_rolls_back_pending_restores_input_and_reconnects(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class ThrowingSendSocket {
          static count=0;
          constructor(){this.instance=++ThrowingSendSocket.count;this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0}),0);
            }
            if(value.type==='message'&&this.instance===1){this.readyState=3;throw new DOMException('socket closed','InvalidStateError')}
            if(value.type==='message'){
              const at=new Date().toISOString(),turn='recovered-send';
              this.emit({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at});
              this.emit({type:'delta',session_id:this.sid,turn_id:turn,sequence:2,at,content:'重连后发送成功'});
              this.emit({type:'done',session_id:this.sid,turn_id:turn,sequence:3,at,completed:true,stop_reason:'stop',usage:null});
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=ThrowingSendSocket;
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("发送时连接关闭")
    page.locator("#prompt").press("Enter")
    page.get_by_text("发送失败，输入内容已恢复；正在重新连接", exact=True).wait_for(timeout=5_000)
    assert page.locator("#prompt").input_value() == "发送时连接关闭"
    assert page.locator(".message.user.pending").count() == 0
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    page.locator("#prompt").press("Enter")
    page.get_by_text("重连后发送成功", exact=True).wait_for(timeout=5_000)


def test_session_relative_time_advances_from_server_clock_offset(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    _connect_and_create(page, web_server)
    baseline = time.time()

    def fixed_server_clock(route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        data = response.json()
        data["server_now"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(baseline))
        activity = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(baseline - 59))
        for session in data.get("sessions", []):
            session["last_activity_at"] = activity
        route.fulfill(
            status=response.status, content_type="application/json", body=json.dumps(data)
        )

    page.route("**/v1/sessions", fixed_server_clock)
    page.reload(wait_until="domcontentloaded")
    page.locator(".session-row.selected").get_by_text("刚刚", exact=True).wait_for(timeout=5_000)
    page.locator(".session-row.selected").get_by_text("1 分钟前", exact=True).wait_for(
        timeout=4_000
    )


def test_model_listbox_escape_and_connection_errors_do_not_disable_input(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class RecoverableSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          send(raw){const m=JSON.parse(raw),emit=x=>this.onmessage?.({data:JSON.stringify(x)});if(m.type==='hello'){this.sid=m.session_id;setTimeout(()=>emit({type:'ready',session_id:this.sid,task_state:'idle'}),0)}else if(m.type==='message'){window.__emitRecoverableError=()=>emit({type:'error',code:'invalid_model',message:'模型已刷新',recoverable:true})}}
          close(){this.readyState=3}
        }
        window.WebSocket=RecoverableSocket;
        """
    )
    _connect_and_create(page, web_server)
    trigger = page.locator(".model-trigger")
    trigger.click()
    assert page.get_by_role("listbox").is_visible()
    page.keyboard.press("Escape")
    assert page.get_by_role("listbox").is_hidden()
    trigger.focus()
    trigger.press("ArrowUp")
    trigger.press("Enter")
    assert page.locator(".model-trigger").is_enabled()
    page.locator("#prompt").fill("触发可恢复请求错误")
    page.locator("#prompt").press("Enter")
    pending = page.locator('.message.user[data-pending="true"]')
    pending.get_by_text("触发可恢复请求错误", exact=True).wait_for(timeout=5_000)
    page.evaluate("window.__emitRecoverableError()")
    page.get_by_text("模型已刷新", exact=True).wait_for(timeout=5_000)
    assert pending.count() == 0
    assert page.locator("#messages").get_by_text("触发可恢复请求错误", exact=True).count() == 0
    assert page.locator("#connectionBadge b").inner_text() == "已连接"
    page.locator("#prompt").fill("仍可发送")
    assert page.locator("#sendButton").is_enabled()


def test_socket_errors_are_bound_to_the_socket_session_and_pending_messages_roll_back(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__isolatedSockets=new Map();
        class IsolatedSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;window.__isolatedSockets.set(this.sid,this);
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0);
            }
            if(value.type==='message') setTimeout(()=>this.emit({type:'error',code:'invalid_model',message:'A 会话可恢复错误',recoverable:true}),100);
          }
          close(){this.readyState=3}
        }
        window.WebSocket=IsolatedSocket;
        window.__emitWrongSession=(socketSid,declaredSid)=>window.__isolatedSockets.get(socketSid).emit({type:'error',session_id:declaredSid,code:'invalid_model',message:'不应路由的错误',recoverable:true});
        """
    )
    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#prompt").fill("不会进入历史的乐观消息")
    page.locator("#prompt").press("Enter")
    page.locator('.message.user[data-pending="true"]').wait_for(timeout=5_000)
    page.get_by_text("A 会话可恢复错误", exact=True).wait_for(timeout=5_000)
    assert page.locator('.message.user[data-pending="true"]').count() == 0
    assert page.locator("#messages").get_by_text("不会进入历史的乐观消息", exact=True).count() == 0

    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    assert page.get_by_role("alert").count() == 0
    page.locator("#prompt").fill("B 的独立草稿")
    page.evaluate(
        "([socketSid,declaredSid])=>window.__emitWrongSession(socketSid,declaredSid)",
        [session_b, session_a],
    )
    page.get_by_text("收到属于其他 Session 的事件，已忽略", exact=True).wait_for(timeout=5_000)
    assert page.locator("#messages .task-card").count() == 0

    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.get_by_text("A 会话可恢复错误", exact=True).wait_for(timeout=5_000)
    assert page.locator("#prompt").input_value() == ""
    page.locator(f'.session-row[data-session-id="{session_b}"]').click()
    assert page.locator("#prompt").input_value() == "B 的独立草稿"
    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.reload(wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)
    assert page.locator("#messages").get_by_text("不会进入历史的乐观消息", exact=True).count() == 0


def test_provider_catalog_failure_stays_in_settings_and_can_recover(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.goto(web_server["web"], wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("未配置", exact=True).wait_for(timeout=5_000)
    assert page.locator(".connection-popover").is_visible()
    page.locator("#newSession").click()
    page.locator("#prompt").fill("没有 Provider 时不能发送")
    assert page.locator("#sendButton").is_disabled()
    page.locator("#providerEndpoint").fill("http://127.0.0.1:1")
    page.locator("#providerApiKey").fill("wrong-provider-key")
    assert page.locator("#saveProvider").is_disabled()
    page.locator("#testProvider").click()
    page.get_by_text("Provider 模型目录不可用：", exact=False).wait_for(timeout=5_000)
    assert page.locator(".connection-popover").is_visible()
    assert page.locator("#saveProvider").is_disabled()
    page.locator("#providerEndpoint").fill(web_server["provider"])
    page.locator("#providerApiKey").fill("provider-test-key")
    page.locator("#providerAuth").select_option("ANTHROPIC_API_KEY")
    page.locator("#testProvider").click()
    page.locator(".provider-tested").wait_for(state="visible", timeout=10_000)
    page.locator("#saveProvider").click()
    page.locator(".connection-popover").wait_for(state="hidden", timeout=10_000)
    assert page.evaluate("localStorage.getItem('oca.provider-auth-env')") == "ANTHROPIC_API_KEY"
    page.locator("#newSession").click()
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)


def test_provider_settings_require_test_before_saving_defaults_and_restore_on_reload(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    session_posts: list[dict[str, object]] = []

    def route_models(route) -> None:
        payload = json.loads(route.request.post_data or "{}")
        if "failed.example" in payload.get("base_url", ""):
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "受控 Provider 连接失败"}),
            )
            return
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "models": ["controlled-model-a", "controlled-model-b"],
                    "default_model": "controlled-model-a",
                }
            ),
        )

    def capture_session(request) -> None:
        if request.method == "POST" and request.url.endswith("/v1/sessions"):
            session_posts.append(json.loads(request.post_data or "{}"))

    page.route("**/v1/web/models", route_models)
    page.on("request", capture_session)
    page.set_viewport_size({"width": 430, "height": 800})
    page.goto(web_server["web"], wait_until="domcontentloaded")
    assert page.locator(".brand strong").inner_text() == "WebAgent"
    assert page.locator(".brand small").inner_text() == "@Username"
    assert page.locator(".topbar h1").inner_text() == "WebAgent"
    assert page.locator("#saveProvider").is_disabled()
    assert page.locator(".provider-tested").count() == 0
    provider_note = page.locator(".connection-popover > p")
    assert (
        provider_note.inner_text() == "先测试 Provider 并读取模型目录；测试不会修改当前已保存连接。"
    )
    note_box = provider_note.bounding_box()
    test_box = page.locator("#testProvider").bounding_box()
    assert note_box["y"] + note_box["height"] <= test_box["y"] - 8
    assert provider_note.evaluate("node => node.scrollWidth <= node.clientWidth")

    page.locator("#providerEndpoint").fill("https://failed.example")
    page.locator("#providerApiKey").fill("draft-provider-key")
    page.locator("#testProvider").click()
    page.get_by_text("受控 Provider 连接失败", exact=True).wait_for(timeout=5_000)
    assert page.locator("#saveProvider").is_disabled()
    assert page.evaluate("localStorage.getItem('oca.provider-endpoint')") is None

    page.locator("#providerEndpoint").fill("https://working.example")
    assert page.locator(".provider-tested").count() == 0
    page.locator("#testProvider").click()
    page.locator(".provider-tested").wait_for(state="visible", timeout=5_000)
    assert page.locator("#providerDefaultModel option").count() == 2
    assert page.locator("#providerDefaultModel").input_value() == "controlled-model-a"
    select_styles = page.locator(
        "#providerAuth, #providerDefaultModel, #providerDefaultEffort"
    ).evaluate_all(
        """
        nodes => nodes.map(node => {const style=getComputedStyle(node);return {
          appearance:style.appearance,webkitAppearance:style.webkitAppearance,
          backgroundImage:style.backgroundImage,borderWidth:style.borderTopWidth,
          borderRadius:style.borderTopLeftRadius,
        }})
        """
    )
    assert len(select_styles) == 3
    assert all(style["appearance"] == "none" for style in select_styles)
    assert all(style["webkitAppearance"] == "none" for style in select_styles)
    assert all(style["backgroundImage"] != "none" for style in select_styles)
    assert all(style["borderWidth"] == "1px" for style in select_styles)
    assert len({style["borderRadius"] for style in select_styles}) == 1
    page.locator("#providerDefaultModel").focus()
    assert page.locator("#providerDefaultModel").evaluate(
        "node => getComputedStyle(node).outlineStyle !== 'none'"
    )
    page.locator("#providerDefaultModel").select_option("controlled-model-b")
    page.locator("#providerDefaultEffort").select_option("high")
    assert page.locator("#saveProvider").is_enabled()
    page.locator("#saveProvider").click()
    assert page.evaluate("localStorage.getItem('oca.default-model')") == "controlled-model-b"
    assert page.evaluate("localStorage.getItem('oca.default-effort')") == "high"

    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator("#newSession").click()
    page.wait_for_function("() => document.querySelector('.session-row.selected')")
    effort_style = page.locator("#effortPicker").evaluate(
        "node => {const style=getComputedStyle(node);return {appearance:style.appearance,webkitAppearance:style.webkitAppearance,backgroundImage:style.backgroundImage,borderWidth:style.borderTopWidth,borderRadius:style.borderTopLeftRadius}}"
    )
    assert effort_style["appearance"] == "none"
    assert effort_style["webkitAppearance"] == "none"
    assert effort_style["backgroundImage"] != "none"
    assert effort_style["borderWidth"] == "1px"
    assert effort_style["borderRadius"] == select_styles[0]["borderRadius"]
    assert session_posts[-1] == {
        "title": "新会话",
        "last_model": "controlled-model-b",
        "last_effort": "high",
    }

    page.get_by_role("button", name="连接设置").click()
    page.locator("#testProvider").click()
    page.locator(".provider-tested").wait_for(state="visible", timeout=5_000)
    page.locator("#providerEndpoint").fill("https://changed.example")
    assert page.locator(".provider-tested").count() == 0
    assert page.locator("#providerDefaultModel").count() == 0
    assert page.locator("#saveProvider").is_disabled()
    page.keyboard.press("Escape")

    page.reload(wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)
    page.locator("#newSession").click()
    page.wait_for_function(
        "count => document.querySelectorAll('.session-row').length >= count", arg=2
    )
    assert session_posts[-1]["last_model"] == "controlled-model-b"
    assert session_posts[-1]["last_effort"] == "high"


def test_created_sessions_appear_immediately_survive_stale_lists_and_fail_cleanly(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__selectedSessionScrolls=0;
        const originalScrollIntoView=Element.prototype.scrollIntoView;
        Element.prototype.scrollIntoView=function(...args){
          if(this.matches?.('.session-row.selected')) window.__selectedSessionScrolls+=1;
          return originalScrollIntoView?.apply(this,args);
        };
        """
    )
    _connect_and_create(page, web_server)
    stale_list = page.evaluate("fetch('/v1/sessions').then(response => response.json())")
    control = {"fail": False, "posts": 0}

    def route_sessions(route) -> None:
        request = route.request
        if request.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(stale_list))
            return
        control["posts"] += 1
        if request.method == "POST" and control["fail"]:
            route.fulfill(
                status=503,
                content_type="application/json",
                body=json.dumps({"detail": "受控创建失败"}),
            )
            return
        response = route.fetch()
        created = response.json()
        created["title"] = ""
        route.fulfill(response=response, json=created)

    page.route("**/v1/sessions", route_sessions)
    rows = page.locator(".session-row")
    before = rows.count()
    scrolls_before = page.evaluate("window.__selectedSessionScrolls")
    page.locator("#newSession").evaluate("button => { button.click(); button.click(); }")
    page.wait_for_function(
        "expected => document.querySelectorAll('.session-row').length === expected",
        arg=before + 1,
    )
    assert control["posts"] == 1
    ids = rows.evaluate_all("items => items.map(item => item.dataset.sessionId)")
    assert len(ids) == len(set(ids)) == before + 1
    selected = page.locator(".session-row.selected")
    assert selected.get_attribute("data-session-id") == page.evaluate(
        "localStorage.getItem('oca.active-session')"
    )
    assert selected.locator("strong").inner_text() == "新会话"
    assert page.evaluate("window.__selectedSessionScrolls") > scrolls_before
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    assert page.locator(".model-trigger").is_enabled()
    assert page.locator("#effortPicker").is_enabled()
    assert page.locator("#prompt").is_enabled()
    page.locator("#effortPicker").select_option("medium")
    page.wait_for_function("document.querySelector('#effortPicker').value === 'medium'")
    page.locator(".model-trigger").click()
    model_options = page.get_by_role("listbox", name="选择模型").get_by_role("option")
    model_options.nth(1).click()
    page.locator("#prompt").fill("创建后立即输入")
    assert page.locator("#prompt").input_value() == "创建后立即输入"

    control["fail"] = True
    before_failure = rows.count()
    page.locator("#newSession").click()
    page.get_by_text("受控创建失败", exact=True).wait_for(timeout=5_000)
    assert control["posts"] == 2
    assert rows.count() == before_failure
    assert page.locator("#newSession").is_enabled()


def test_two_sessions_run_concurrently_with_isolated_sockets_and_events(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__sessionSockets=[];
        window.__messagePayloads=[];
        class MultiSessionSocket {
          constructor(){this.readyState=0;this.closed=false;window.__sessionSockets.push(this);setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(payload){this.onmessage?.({data:JSON.stringify(payload)})}
          send(raw){
            const message=JSON.parse(raw);
            if(message.type==='hello'){
              this.sid=message.session_id;
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0);
            }
            if(message.type!=='message') return;
            window.__messagePayloads.push(message);
            this.turn=`turn-${this.sid}`;
            this.emit({type:'turn_started',session_id:this.sid,turn_id:this.turn,sequence:1,at:new Date().toISOString()});
            if(window.__messagePayloads.length===1){
              window.__backgroundA=this;
              this.emit({type:'progress',session_id:this.sid,turn_id:this.turn,sequence:2,at:new Date().toISOString(),phase:'command',message:'A 正在后台执行',status:'running',tool_use_id:'a-tool'});
            }else{
              this.emit({type:'delta',session_id:this.sid,turn_id:this.turn,sequence:2,at:new Date().toISOString(),content:'B 独立完成'});
              this.emit({type:'done',session_id:this.sid,turn_id:this.turn,sequence:3,at:new Date().toISOString(),completed:true,stop_reason:'stop',usage:null});
            }
          }
          close(){this.closed=true;this.readyState=3}
        }
        window.__finishBackgroundA=()=>{
          const socket=window.__backgroundA;
          socket.emit({type:'delta',session_id:socket.sid,turn_id:socket.turn,sequence:3,at:new Date().toISOString(),content:'A 独立完成'});
          socket.emit({type:'done',session_id:socket.sid,turn_id:socket.turn,sequence:4,at:new Date().toISOString(),completed:true,stop_reason:'stop',usage:null});
        };
        window.WebSocket=MultiSessionSocket;
        """
    )
    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#prompt").fill("A 后台任务")
    page.locator("#prompt").press("Enter")
    page.get_by_text("A 正在后台执行", exact=True).wait_for(timeout=5_000)
    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    assert page.evaluate("window.__sessionSockets.filter(socket => !socket.closed).length") == 2
    page.locator("#prompt").fill("B 前台任务")
    page.locator("#prompt").press("Enter")
    page.locator("#messages").get_by_text("B 独立完成", exact=True).wait_for(timeout=5_000)
    page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)
    assert page.locator("#messages").get_by_text("A 独立完成", exact=True).count() == 0
    a_row = page.locator(".session-row").filter(has_text="A 后台任务")
    a_row.get_by_text("运行中", exact=True).wait_for(timeout=5_000)
    page.locator("#prompt").fill("B 中尚未发送的草稿")
    page.evaluate("window.__finishBackgroundA()")
    a_row.get_by_text("待命", exact=True).wait_for(timeout=5_000)
    assert page.evaluate("localStorage.getItem('oca.active-session')") == session_b
    assert page.locator("#prompt").input_value() == "B 中尚未发送的草稿"
    assert page.locator("#messages").get_by_text("A 独立完成", exact=True).count() == 0
    assert "B 独立完成" in page.locator("#messages").inner_text()
    page.wait_for_function("window.__sessionSockets.filter(socket => !socket.closed).length === 1")
    payloads = page.evaluate("window.__messagePayloads")
    assert len(payloads) == 2
    assert all(
        payload["provider"]
        == {
            "base_url": web_server["provider"],
            "api_key": "provider-test-key",
            "auth_env": "ANTHROPIC_AUTH_TOKEN",
        }
        for payload in payloads
    )


def test_reload_restores_two_background_turns_and_keeps_replay_completion_isolated(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    history_requests: list[str] = []
    page.on(
        "request",
        lambda request: (
            history_requests.append(request.url) if request.url.endswith("/history") else None
        ),
    )
    page.add_init_script(
        """
        window.__backgroundSockets=new Map();
        const journalKey=sid=>`__backgroundJournal:${sid}`;
        const readJournal=sid=>JSON.parse(localStorage.getItem(journalKey(sid))||'[]');
        const writeJournal=(sid,events)=>localStorage.setItem(journalKey(sid),JSON.stringify(events));
        const activeIds=()=>new Set(JSON.parse(localStorage.getItem('__backgroundActive')||'[]'));
        const finishingIds=()=>new Set(JSON.parse(localStorage.getItem('__backgroundFinishing')||'[]'));
        const setActive=ids=>localStorage.setItem('__backgroundActive',JSON.stringify([...ids]));
        class BackgroundReplaySocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(value){this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;window.__backgroundSockets.set(this.sid,this);
              return setTimeout(()=>{
                this.emit({type:'sync_begin',session_id:this.sid});
                const journal=readJournal(this.sid);journal.forEach(event=>this.emit(event));
                const running=activeIds().has(this.sid),finishing=finishingIds().has(this.sid),turn=`background-${this.sid}`;
                this.emit({type:'ready',session_id:this.sid,task_state:running?(finishing?'finishing':'running'):'idle',turn_id:running?turn:null,last_sequence:running?2:0});
              },0);
            }
            if(value.type==='message'){
              const turn=`background-${this.sid}`,at=new Date().toISOString(),events=[
                {type:'user_message',session_id:this.sid,turn_id:turn,sequence:0,at,content:value.content},
                {type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at},
                {type:'progress',session_id:this.sid,turn_id:turn,sequence:2,at,phase:'tool',status:'running',message:`后台执行 ${value.content}`,tool_use_id:`tool-${this.sid}`},
              ];
              writeJournal(this.sid,events);const ids=activeIds();ids.add(this.sid);setActive(ids);events.forEach(event=>this.emit(event));
            }
            if(value.type==='stop'){
              window.__backgroundStoppedTurn=value.turn_id;
              window.__finishBackgroundSession(this.sid,'已按要求停止','stopped');
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=BackgroundReplaySocket;
        window.__finishBackgroundSession=(sid,text,reason='stop')=>{
          const socket=window.__backgroundSockets.get(sid),journal=readJournal(sid),turn=`background-${sid}`,at=new Date().toISOString();
          const tail=[
            {type:'delta',session_id:sid,turn_id:turn,sequence:3,at,content:text},
            {type:'done',session_id:sid,turn_id:turn,sequence:4,at,completed:reason==='stop',stop_reason:reason,usage:null},
          ];
          writeJournal(sid,[...journal,...tail]);const ids=activeIds();ids.delete(sid);setActive(ids);tail.forEach(event=>socket.emit(event));
        };
        """
    )
    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#prompt").fill("A 长任务")
    page.locator("#prompt").press("Enter")
    page.get_by_text("后台执行 A 长任务", exact=True).wait_for(timeout=5_000)
    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    page.locator("#prompt").fill("B 长任务")
    page.locator("#prompt").press("Enter")
    page.get_by_text("后台执行 B 长任务", exact=True).wait_for(timeout=5_000)

    working = {session_a, session_b}
    finishing = {session_a}
    page.evaluate(
        "ids => localStorage.setItem('__backgroundFinishing', JSON.stringify(ids))",
        list(finishing),
    )

    def route_sessions(route) -> None:
        if route.request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        data = response.json()
        for session in data.get("sessions", []):
            sid = session.get("session_id")
            session["task_state"] = (
                "finishing" if sid in finishing else "running" if sid in working else "idle"
            )
            session["active_turn_id"] = f"background-{sid}" if sid in working else None
            session["last_turn_sequence"] = 2 if sid in working else 0
        route.fulfill(
            status=response.status, content_type="application/json", body=json.dumps(data)
        )

    page.route("**/v1/sessions", route_sessions)
    page.reload(wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)
    page.wait_for_function(
        "ids => ids.every(id => window.__backgroundSockets.has(id))",
        arg=[session_a, session_b],
    )
    assert page.evaluate("localStorage.getItem('oca.active-session')") == session_b
    assert page.locator(".message.user").count() == 1
    assert "后台执行 B 长任务" in page.locator("#messages").inner_text()
    assert page.locator("#messages").get_by_text("后台执行 A 长任务", exact=True).count() == 0
    assert (
        page.locator(f'.session-row[data-session-id="{session_a}"]')
        .get_by_text("运行中", exact=True)
        .is_visible()
    )
    assert (
        page.locator(f'.session-row[data-session-id="{session_b}"]')
        .get_by_text("运行中", exact=True)
        .is_visible()
    )

    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.get_by_text("正在完成", exact=False).first.wait_for(timeout=5_000)
    assert page.locator(".task-card .stop-inline").count() == 0
    assert page.locator("#sendButton").is_disabled()
    page.locator(f'.session-row[data-session-id="{session_b}"]').click()
    page.get_by_text("后台执行 B 长任务", exact=True).wait_for(timeout=5_000)

    working.remove(session_a)
    finishing.remove(session_a)
    page.evaluate("sid => window.__finishBackgroundSession(sid,'A 后台完成')", session_a)
    page.locator(f'.session-row[data-session-id="{session_a}"]').get_by_text(
        "待命", exact=True
    ).wait_for(timeout=5_000)
    assert page.evaluate("localStorage.getItem('oca.active-session')") == session_b
    assert page.locator("#messages").get_by_text("A 后台完成", exact=True).count() == 0
    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.get_by_text("A 后台完成", exact=True).wait_for(timeout=5_000)
    assert page.locator(".message.user").count() == 1
    page.locator(f'.session-row[data-session-id="{session_b}"]').click()
    page.get_by_text("后台执行 B 长任务", exact=True).wait_for(timeout=5_000)

    working.remove(session_b)
    page.get_by_role("button", name="停止").last.click()
    page.get_by_text("已停止", exact=False).first.wait_for(timeout=5_000)
    assert page.evaluate("window.__backgroundStoppedTurn") == f"background-{session_b}"
    assert page.locator("#messages").get_by_text("A 后台完成", exact=True).count() == 0
    assert history_requests == []


def test_session_effort_is_sent_locked_per_turn_and_persisted_independently(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__effortPayloads=[];
        window.__finishEffortTurn=()=>{
          const pending=window.__pendingEffortTurn;
          if(!pending) return;
          window.__pendingEffortTurn=null;
          const {socket,turn,index}=pending, at=new Date().toISOString();
          socket.emit({type:'delta',session_id:socket.sid,turn_id:turn,sequence:3,at,content:`effort turn ${index} done`});
          socket.emit({type:'done',session_id:socket.sid,turn_id:turn,sequence:4,at,completed:true,stop_reason:'stop',usage:null});
        };
        class EffortSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(payload){this.onmessage?.({data:JSON.stringify(payload)})}
          send(raw){
            const message=JSON.parse(raw);
            if(message.type==='hello'){
              this.sid=message.session_id;
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0);
            }
            if(message.type!=='message') return;
            const index=window.__effortPayloads.push({...message,session_id:this.sid});
            const turn=`effort-${this.sid}-${index}`, at=new Date().toISOString();
            this.emit({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:2,at,phase:'task',status:'started',message:'执行 effort 任务',task_id:`effort-task-${index}`});
            window.__pendingEffortTurn={socket:this,turn,index};
          }
          close(){this.readyState=3}
        }
        window.WebSocket=EffortSocket;
        """
    )
    _connect_and_create(page, web_server)
    effort = page.locator("#effortPicker")
    assert effort.input_value() == ""

    effort.select_option("medium")
    page.wait_for_function("document.querySelector('#effortPicker').value === 'medium'")
    page.locator("#prompt").fill("A medium first")
    page.locator("#prompt").press("Enter")
    page.wait_for_function("window.__effortPayloads.length === 1")
    page.locator(".task-card").wait_for(state="visible")
    assert effort.is_disabled()
    assert page.evaluate("window.__effortPayloads[0].effort") == "medium"
    page.evaluate("window.__finishEffortTurn()")
    page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)
    assert effort.is_enabled()

    effort.select_option("high")
    page.wait_for_function("document.querySelector('#effortPicker').value === 'high'")
    page.locator("#prompt").fill("A high second")
    page.locator("#prompt").press("Enter")
    page.wait_for_function("window.__effortPayloads.length === 2")
    assert effort.is_disabled()
    assert page.evaluate("window.__effortPayloads[1].effort") == "high"
    page.evaluate("window.__finishEffortTurn()")
    page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)

    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=session_a
    )
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    assert effort.input_value() == ""

    page.locator(".session-row").filter(has_text="A medium first").click()
    page.wait_for_function("document.querySelector('#effortPicker').value === 'high'")
    page.reload(wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)
    assert page.locator("#effortPicker").input_value() == "high"


def test_live_narration_moves_into_following_steps_and_leaves_final_reply(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class NarrationSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(payload){this.onmessage?.({data:JSON.stringify(payload)})}
          send(raw){
            const message=JSON.parse(raw);
            if(message.type==='hello'){
              this.sid=message.session_id;
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0);
            }
            if(message.type!=='message') return;
            const turn='narrated-turn', at=()=>new Date().toISOString();
            this.emit({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at:at()});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:2,at:at(),phase:'analysis',status:'started',message:'Agent/分析',tool_use_id:'analysis-1'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:3,at:at(),phase:'analysis',status:'completed',message:'Agent/分析',tool_use_id:'analysis-1'});
            this.emit({type:'delta',session_id:this.sid,turn_id:turn,sequence:4,at:at(),content:'    const indentation = true;\\n\\n## 说明A\\n\\n- **重点**\\n\\n> 引用'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:5,at:at(),phase:'tool',status:'started',message:'Write',tool_name:'Write',tool_use_id:'write-tool'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:6,at:at(),phase:'tool',status:'completed',message:'Write',tool_name:'Write',tool_use_id:'write-tool'});
            this.emit({type:'delta',session_id:this.sid,turn_id:turn,sequence:7,at:at(),content:'## 说明B\\n\\n[文档](https://example.com)\\n\\n| 项 | 值 |\\n| --- | --- |\\n| B | 2 |'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:8,at:at(),phase:'task',status:'started',message:'Bash · Bash',tool_name:'Bash',task_id:'bash-task'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:9,at:at(),phase:'task',status:'completed',message:'Bash · Bash',tool_name:'Bash',task_id:'bash-task'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:10,at:at(),phase:'finalizing',status:'started',message:'finalizing',tool_use_id:'finalizing-1'});
            this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence:11,at:at(),phase:'finalizing',status:'completed',message:'finalizing',tool_use_id:'finalizing-1'});
            this.emit({type:'delta',session_id:this.sid,turn_id:turn,sequence:12,at:at(),content:'## 最终总结\\n\\n使用 `inline`\\n\\n```js\\nconst ok = true;\\n```'});
            this.emit({type:'done',session_id:this.sid,turn_id:turn,sequence:13,at:at(),completed:true,stop_reason:'stop',usage:null});
          }
          close(){this.readyState=3}
        }
        window.WebSocket=NarrationSocket;
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("用户 **原样**")
    page.locator("#prompt").press("Enter")
    page.locator(".message.assistant .message-text h2").get_by_text(
        "最终总结", exact=True
    ).wait_for(timeout=5_000)

    timeline = page.locator(".task-card .steps")
    narrations = timeline.locator(":scope > .narration-row")
    assert narrations.count() == 2
    assert narrations.nth(0).locator("pre code").inner_text().strip() == "const indentation = true;"
    assert narrations.nth(0).locator("h2").inner_text() == "说明A"
    assert narrations.nth(0).locator("ul strong").inner_text() == "重点"
    assert narrations.nth(0).locator("blockquote").is_visible()
    assert narrations.nth(1).locator("h2").inner_text() == "说明B"
    assert narrations.nth(1).locator("table").is_visible()
    assert narrations.nth(1).locator("a").get_attribute("href") == "https://example.com"
    assert page.locator(".step-note").count() == 0
    assert narrations.locator("button").count() == 0
    assert page.evaluate(
        "[...document.querySelectorAll('.narration-row')].every(row => row.parentElement.classList.contains('steps') && !row.closest('.step-wrap'))"
    )

    write_step = timeline.locator(":scope > .step-wrap").filter(has_text="Write")
    bash_step = timeline.locator(":scope > .step-wrap").filter(has_text="Bash")
    assert write_step.locator(".step-row button").inner_text().startswith("活动 ")
    assert bash_step.locator(".step-row button").inner_text().startswith("活动 ")
    write_x = write_step.locator(".step-row strong").bounding_box()["x"]
    bash_x = bash_step.locator(".step-row strong").bounding_box()["x"]
    narration_a_x = narrations.nth(0).locator("h2").bounding_box()["x"]
    narration_b_x = narrations.nth(1).locator("h2").bounding_box()["x"]
    assert abs(narration_a_x - write_x) <= 2
    assert abs(narration_b_x - bash_x) <= 2
    visual = page.evaluate(
        """
        () => {
          const narration=document.querySelectorAll('.narration-row')[1].querySelector('p');
          const labels=[...document.querySelectorAll('.phase-tool .step-row > strong, .phase-task .step-row > strong')];
          const colorValue=value => (value.match(/\\d+/g)||[]).slice(0,3).reduce((sum,item)=>sum+Number(item),0);
          const narrationStyle=getComputedStyle(narration),labelStyles=labels.map(label=>getComputedStyle(label));
          const node=document.querySelector('.narration-node').getBoundingClientRect();
          return {
            narrationWeight:Number(narrationStyle.fontWeight),
            narrationColor:colorValue(narrationStyle.color),
            labelWeights:labelStyles.map(style=>Number(style.fontWeight)),
            labelColors:labelStyles.map(style=>colorValue(style.color)),
            nodeWidth:node.width,
            nodeHeight:node.height,
          };
        }
        """
    )
    assert visual["narrationWeight"] >= 600
    assert all(weight <= 500 for weight in visual["labelWeights"])
    assert all(visual["narrationColor"] < color for color in visual["labelColors"])
    assert 8 <= visual["nodeWidth"] <= 10
    assert 8 <= visual["nodeHeight"] <= 10
    write_step.locator(".step-row button").click()
    assert write_step.locator(".activities").is_visible()
    assistant_replies = page.locator(".message.assistant .message-text")
    assert assistant_replies.count() == 1
    assert assistant_replies.locator("h2").inner_text() == "最终总结"
    assert assistant_replies.locator("code").filter(has_text="inline").is_visible()
    assert assistant_replies.locator("pre code").inner_text().strip() == "const ok = true;"
    user_message = page.locator(".message.user .message-text")
    assert "**原样**" in user_message.inner_text()
    assert user_message.locator("strong").count() == 0
    assert page.evaluate(
        """
        () => {
          const timeline=document.querySelector('.task-card .steps');
          const rows=[...timeline.children].map(row => row.matches('.narration-row')
            ? `narration:${row.querySelector('h2')?.textContent}`
            : row.querySelector('.step-row strong')?.textContent);
          const expected=['Agent/分析','narration:说明A','Write','narration:说明B','Bash','finalizing'];
          const final=document.querySelector('.message.assistant .message-text h2');
          const nodes=[...timeline.children,final];
          return JSON.stringify(rows)===JSON.stringify(expected) && nodes.every(Boolean) && nodes.slice(0,-1).every(
            (node,index) => Boolean(node.compareDocumentPosition(nodes[index+1]) & Node.DOCUMENT_POSITION_FOLLOWING)
          );
        }
        """
    )
    assert "Bash · Bash" not in page.locator(".task-card").inner_text()
    assert "Write · Write" not in page.locator(".task-card").inner_text()


def test_phase_specific_progress_ids_and_parent_children_do_not_merge(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class StepIdentitySocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(payload){this.onmessage?.({data:JSON.stringify(payload)})}
          send(raw){
            const message=JSON.parse(raw);
            if(message.type==='hello'){
              this.sid=message.session_id;
              return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0);
            }
            if(message.type!=='message') return;
            const turn='identity-turn', at=()=>new Date().toISOString(), progress=(sequence,payload)=>
              this.emit({type:'progress',session_id:this.sid,turn_id:turn,sequence,at:at(),status:'started',...payload});
            this.emit({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at:at()});
            progress(2,{phase:'tool',message:'Write',tool_use_id:'common-tool',task_id:'common-task'});
            progress(3,{phase:'task',message:'Bash',tool_use_id:'common-tool',task_id:'common-task'});
            progress(4,{phase:'task',message:'Verify',tool_use_id:'common-tool',task_id:'verify-task'});
            progress(5,{phase:'tool',message:'Read',tool_use_id:'child-read',task_id:'common-task',parent_tool_use_id:'common-tool'});
            this.emit({type:'done',session_id:this.sid,turn_id:turn,sequence:6,at:at(),completed:true,stop_reason:'stop',usage:null});
          }
          close(){this.readyState=3}
        }
        window.WebSocket=StepIdentitySocket;
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("检查步骤标识")
    page.locator("#prompt").press("Enter")
    page.get_by_text("任务完成", exact=False).first.wait_for(timeout=5_000)
    assert (
        page.locator(".message.assistant .message-copy > header strong").first.inner_text()
        == "WebAgent"
    )
    page.get_by_role("button", name="……已折叠 2 个……").click()
    steps = page.locator(".task-card .steps > .step-wrap")
    assert steps.count() == 4
    assert [steps.nth(index).locator(".step-row strong").inner_text() for index in range(4)] == [
        "Write",
        "Bash",
        "Verify",
        "Read",
    ]
    assert all(
        "nested" not in (steps.nth(index).get_attribute("class") or "") for index in range(4)
    )
    label_x = [
        steps.nth(index).locator(".step-row strong").bounding_box()["x"] for index in range(4)
    ]
    assert max(label_x) - min(label_x) <= 2
    assert page.locator(".narration-row").count() == 0


def test_completed_tool_segments_fold_expand_and_replay_from_history(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    persisted: dict[str, list[dict[str, object]]] = {"events": []}

    def route_history(route) -> None:
        session_id = route.request.url.split("/v1/sessions/", 1)[1].split("/history", 1)[0]
        events = [event for event in persisted["events"] if event["session_id"] == session_id]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session_id": session_id, "events": events}),
        )

    page.route("**/v1/sessions/*/history", route_history)
    page.add_init_script(
        """
        window.__foldHistory=[];
        class FoldSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          deliver(value){window.__foldHistory.push(value);this.onmessage?.({data:JSON.stringify(value)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;
              return setTimeout(()=>{
                this.onmessage?.({data:JSON.stringify({type:'sync_begin',session_id:this.sid})});
                const replay=JSON.parse(localStorage.getItem('__foldReplay')||'[]').filter(event=>event.session_id===this.sid);
                replay.forEach(event=>this.onmessage?.({data:JSON.stringify(event)}));
                this.onmessage?.({data:JSON.stringify({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0})});
              },0)
            }
            if(value.type!=='message') return;
            const turn='fold-turn',at=()=>new Date().toISOString();let sequence=0;
            window.__foldHistory.push({type:'user_message',session_id:this.sid,turn_id:turn,sequence:sequence++,at:at(),content:value.content});
            this.deliver({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:sequence++,at:at()});
            const progress=(phase,message,id,status='completed')=>this.deliver({type:'progress',session_id:this.sid,turn_id:turn,sequence:sequence++,at:at(),phase,message,status,tool_use_id:id,task_id:`task-${id}`});
            progress('starting','沙箱准备完成','start');
            progress('tool','Fold-A','a');
            progress('task','Fold-B','b');
            progress('tool','Fold-C','c');
            progress('task','Fold-D','d');
            progress('analysis','分析完成','analysis');
            this.deliver({type:'delta',session_id:this.sid,turn_id:turn,sequence:sequence++,at:at(),content:'分隔说明'});
            progress('tool','Fold-E','e','started');
            progress('tool','Fold-E','e','completed');
            this.deliver({type:'done',session_id:this.sid,turn_id:turn,sequence:sequence++,at:at(),completed:true,stop_reason:'stop',usage:null});
          }
          close(){this.readyState=3}
        }
        window.WebSocket=FoldSocket;
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("检查连续步骤折叠")
    page.locator("#prompt").press("Enter")
    page.get_by_text("任务完成", exact=False).first.wait_for(timeout=5_000)

    folded = page.get_by_role("button", name="……已折叠 2 个……")
    assert folded.is_visible()
    assert folded.get_attribute("aria-expanded") == "false"
    assert page.get_by_text("Fold-A", exact=True).is_visible()
    assert page.get_by_text("Fold-D", exact=True).is_visible()
    assert page.get_by_text("Fold-B", exact=True).count() == 0
    assert page.get_by_text("Fold-C", exact=True).count() == 0
    assert page.get_by_text("分隔说明", exact=True).is_visible()
    assert page.get_by_text("Fold-E", exact=True).is_visible()

    folded.click()
    collapse = page.get_by_role("button", name="收起 2 个")
    assert collapse.is_visible()
    segment_labels = page.locator(".task-card .step-row strong").all_inner_texts()
    indices = [segment_labels.index(label) for label in ("Fold-A", "Fold-B", "Fold-C", "Fold-D")]
    assert indices == sorted(indices)
    middle_step = page.locator(".step-wrap").filter(has_text="Fold-B")
    middle_step.get_by_role("button", name="活动 1").click()
    assert middle_step.locator(".activities").is_visible()
    collapse.click()
    assert page.get_by_role("button", name="……已折叠 2 个……").is_visible()
    assert page.get_by_text("Fold-B", exact=True).count() == 0

    persisted["events"] = page.evaluate("window.__foldHistory")
    page.evaluate(
        "events => localStorage.setItem('__foldReplay', JSON.stringify(events))",
        persisted["events"],
    )
    page.reload(wait_until="domcontentloaded")
    page.get_by_role("button", name="……已折叠 2 个……").wait_for(timeout=10_000)
    assert page.get_by_text("Fold-B", exact=True).count() == 0
    page.get_by_role("button", name="……已折叠 2 个……").click()
    assert page.get_by_text("Fold-B", exact=True).is_visible()
    assert page.get_by_text("Fold-C", exact=True).is_visible()


def test_deleted_sessions_are_hidden_and_never_restored_as_active_selection(
    web_server: dict[str, str], browser_page: Page, tmp_path: Path
) -> None:
    page = browser_page
    _connect_and_create(page, web_server)
    active_id = page.evaluate("localStorage.getItem('oca.active-session')")
    created = page.evaluate(
        """
        async ({activeId}) => {
          const json = (path, options={}) => fetch(path, {
            ...options,
            headers: {'Content-Type':'application/json', ...(options.headers||{})},
          }).then(response => response.json());
          await json(`/v1/sessions/${activeId}`, {
            method:'PATCH', body:JSON.stringify({title:'Active visible'}),
          });
          const paused = await json('/v1/sessions', {
            method:'POST', body:JSON.stringify({title:'Paused visible'}),
          });
          await json(`/v1/sessions/${paused.session_id}/pause`, {method:'POST'});
          const deleted = await json('/v1/sessions', {
            method:'POST', body:JSON.stringify({title:'Deleted hidden'}),
          });
          await json(`/v1/sessions/${deleted.session_id}`, {method:'DELETE'});
          const incompatible = await json('/v1/sessions', {
            method:'POST', body:JSON.stringify({title:'Incompatible hidden'}),
          });
          return {
            pausedId:paused.session_id,
            deletedId:deleted.session_id,
            incompatibleId:incompatible.session_id,
          };
        }
        """,
        {"activeId": active_id},
    )
    with sqlite3.connect(tmp_path / "browser.db") as database:
        row = database.execute(
            "SELECT metadata_json FROM sessions WHERE session_id = ?",
            (created["incompatibleId"],),
        ).fetchone()
        metadata = json.loads(row[0])
        metadata["runtime_backend"] = "ZhipuRuntime"
        database.execute(
            "UPDATE sessions SET metadata_json = ? WHERE session_id = ?",
            (json.dumps(metadata), created["incompatibleId"]),
        )
    # Simulate a stale browser preference that points at a legacy incompatible Session.
    page.evaluate("id => localStorage.setItem('oca.active-session', id)", created["incompatibleId"])
    page.reload(wait_until="domcontentloaded")
    rail = page.locator(".session-rail")
    rail.get_by_text("Active visible", exact=True).wait_for(timeout=10_000)
    rail.get_by_text("Paused visible", exact=True).wait_for(timeout=10_000)
    assert rail.get_by_text("Deleted hidden", exact=True).count() == 0
    assert rail.get_by_text("Incompatible hidden", exact=True).count() == 0
    assert rail.get_by_text("待命", exact=True).count() >= 1
    assert rail.get_by_text("已暂停", exact=True).count() >= 1
    selected_id = page.evaluate("localStorage.getItem('oca.active-session')")
    assert selected_id not in {created["deletedId"], created["incompatibleId"]}
    assert "Deleted hidden" not in page.locator(".session-row.selected").inner_text()
    assert "Incompatible hidden" not in page.locator(".session-row.selected").inner_text()


def test_provider_test_ignores_stale_response_after_draft_changes(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__modelRequests=[];
        const nativeFetch=window.fetch.bind(window);
        window.fetch=(input,init={})=>{
          if(String(input).endsWith('/v1/web/models')){
            const body=JSON.parse(init.body||'{}');
            return new Promise(resolve=>window.__modelRequests.push({body,resolve}));
          }
          return nativeFetch(input,init);
        };
        window.__resolveModelRequest=(index,payload,status=200)=>{
          window.__modelRequests[index].resolve(new Response(JSON.stringify(payload),{
            status,headers:{'Content-Type':'application/json'}
          }));
        };
        """
    )
    page.goto(web_server["web"], wait_until="domcontentloaded")
    page.locator("#providerEndpoint").fill("https://first.example")
    page.locator("#providerApiKey").fill("first-key")
    page.locator("#testProvider").click()
    page.wait_for_function("window.__modelRequests.length === 1")

    # Editing invalidates the pending test and permits a new test immediately.
    page.locator("#providerEndpoint").fill("https://second.example")
    assert page.locator("#testProvider").is_enabled()
    page.locator("#testProvider").click()
    page.wait_for_function("window.__modelRequests.length === 2")
    page.evaluate(
        "window.__resolveModelRequest(0,{models:['stale-model'],default_model:'stale-model'})"
    )
    page.wait_for_timeout(100)
    assert page.locator(".provider-tested").count() == 0
    assert page.locator("#saveProvider").is_disabled()
    assert page.locator("#testProvider").inner_text() == "正在测试…"

    page.evaluate(
        "window.__resolveModelRequest(1,{models:['fresh-model'],default_model:'fresh-model'})"
    )
    page.locator(".provider-tested").wait_for(state="visible", timeout=5_000)
    assert page.locator("#providerDefaultModel").input_value() == "fresh-model"
    assert page.locator("#saveProvider").is_enabled()


def test_late_startup_provider_catalog_cannot_overwrite_saved_new_provider(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        localStorage.setItem('oca.provider-endpoint','https://old.example');
        localStorage.setItem('oca.provider-api-key','old-key');
        localStorage.setItem('oca.provider-auth-env','ANTHROPIC_AUTH_TOKEN');
        localStorage.setItem('oca.default-model','old-model');
        window.__startupCatalogs=[];
        const nativeFetch=window.fetch.bind(window);
        window.fetch=(input,init={})=>{
          if(String(input).endsWith('/v1/web/models')){
            const body=JSON.parse(init.body||'{}');
            return new Promise(resolve=>window.__startupCatalogs.push({body,resolve}));
          }
          return nativeFetch(input,init);
        };
        window.__resolveStartupCatalog=(index,payload)=>window.__startupCatalogs[index].resolve(
          new Response(JSON.stringify(payload),{status:200,headers:{'Content-Type':'application/json'}})
        );
        """
    )
    page.goto(web_server["web"], wait_until="domcontentloaded")
    page.wait_for_function("window.__startupCatalogs.length === 1")
    page.get_by_role("button", name="连接设置").click()
    page.locator("#providerEndpoint").fill("https://new.example")
    page.locator("#providerApiKey").fill("new-key")
    page.locator("#testProvider").click()
    page.wait_for_function("window.__startupCatalogs.length === 2")
    page.evaluate(
        "window.__resolveStartupCatalog(1,{models:['new-a','new-b'],default_model:'new-a'})"
    )
    page.locator(".provider-tested").wait_for(state="visible", timeout=5_000)
    page.locator("#providerDefaultModel").select_option("new-b")
    page.locator("#saveProvider").click()
    page.evaluate(
        "window.__resolveStartupCatalog(0,{models:['old-model'],default_model:'old-model'})"
    )
    page.wait_for_timeout(100)
    assert page.evaluate("localStorage.getItem('oca.provider-endpoint')") == "https://new.example"
    assert page.evaluate("localStorage.getItem('oca.default-model')") == "new-b"
    page.locator("#newSession").click()
    page.wait_for_function("document.querySelector('.session-row.selected') !== null")
    assert "new-b" in page.locator(".model-trigger").inner_text()
    page.locator(".model-trigger").click()
    options = page.get_by_role("listbox", name="选择模型").get_by_role("option")
    assert options.all_inner_texts() == ["new-a", "new-b"]


def test_existing_session_with_model_outside_catalog_requires_explicit_reselection(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    _connect_and_create(page, web_server)
    session_id = page.evaluate("localStorage.getItem('oca.active-session')")
    page.evaluate(
        """
        ({sessionId}) => fetch(`/v1/sessions/${sessionId}`, {
          method:'PATCH', headers:{'Content-Type':'application/json'},
          body:JSON.stringify({last_model:'legacy-provider-model'})
        }).then(response => response.json())
        """,
        {"sessionId": session_id},
    )
    page.reload(wait_until="domcontentloaded")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=10_000)
    trigger = page.locator(".model-trigger")
    assert "legacy-provider-model" in trigger.inner_text()
    assert trigger.get_attribute("aria-invalid") == "true"
    assert trigger.is_enabled()
    page.locator("#prompt").fill("旧模型不能静默发送")
    assert page.locator("#sendButton").is_disabled()

    trigger.click()
    page.get_by_role("option", name="browser-fake-model").click()
    page.wait_for_function(
        "document.querySelector('.model-trigger strong').textContent === 'browser-fake-model'"
    )
    assert page.locator(".model-trigger").get_attribute("aria-invalid") is None
    assert page.locator("#sendButton").is_enabled()


def test_session_context_menu_delete_cancel_nonactive_and_active_switch(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    _connect_and_create(page, web_server)
    first_id = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=first_id
    )
    second_id = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=second_id
    )
    third_id = page.evaluate("localStorage.getItem('oca.active-session')")
    page.evaluate(
        """
        async entries => {
          for(const [sessionId,title] of entries){
            await fetch(`/v1/sessions/${sessionId}`,{
              method:'PATCH',headers:{'Content-Type':'application/json'},
              body:JSON.stringify({title})
            });
          }
        }
        """,
        [[first_id, "First session"], [second_id, "Second session"], [third_id, "Third session"]],
    )
    page.reload(wait_until="domcontentloaded")
    page.locator(".topbar h1").get_by_text("Third session", exact=True).wait_for(timeout=10_000)
    deleted_urls: list[str] = []

    def capture_delete(request) -> None:
        if request.method == "DELETE":
            deleted_urls.append(request.url)

    page.on("request", capture_delete)
    second_row = page.locator(f'.session-row[data-session-id="{second_id}"]')
    second_row.click(button="right", position={"x": 8, "y": 8})
    menu = page.get_by_role("menu")
    box = menu.bounding_box()
    assert box and box["x"] >= 0 and box["y"] >= 0
    assert box["x"] + box["width"] <= 1440
    assert box["y"] + box["height"] <= 900
    page.keyboard.press("Escape")
    assert menu.is_hidden()

    second_row.click(button="right")
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("menuitem", name="删除会话").click()
    assert second_row.is_visible()
    assert deleted_urls == []

    second_row.click(button="right")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("menuitem", name="删除会话").click()
    second_row.wait_for(state="detached", timeout=5_000)
    assert deleted_urls[-1].endswith(f"/v1/sessions/{second_id}")
    assert page.locator(".topbar h1").inner_text() == "Third session"

    third_row = page.locator(f'.session-row[data-session-id="{third_id}"]')
    third_row.click(button="right")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("menuitem", name="删除会话").click()
    third_row.wait_for(state="detached", timeout=5_000)
    assert deleted_urls[-1].endswith(f"/v1/sessions/{third_id}")
    page.locator(".topbar h1").get_by_text("First session", exact=True).wait_for(timeout=5_000)
    assert page.evaluate("localStorage.getItem('oca.active-session')") == first_id


def test_slow_session_creation_disables_composer_and_cannot_send_to_old_socket(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        window.__sentDuringCreate=[];
        class CreateGuardSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          send(raw){
            const value=JSON.parse(raw),emit=payload=>this.onmessage?.({data:JSON.stringify(payload)});
            if(value.type==='hello'){this.sid=value.session_id;setTimeout(()=>emit({type:'ready',session_id:this.sid,task_state:'idle'}),0)}
            else if(value.type==='message') window.__sentDuringCreate.push({...value,session_id:this.sid});
          }
          close(){this.readyState=3}
        }
        window.WebSocket=CreateGuardSocket;
        """
    )
    _connect_and_create(page, web_server)
    old_id = page.evaluate("localStorage.getItem('oca.active-session')")
    page.evaluate(
        """
        () => {
          const nativeFetch=window.fetch.bind(window);
          window.fetch=(input,init={})=>{
            if(String(input).endsWith('/v1/sessions') && init.method==='POST'){
              return new Promise(resolve=>{window.__finishSlowCreate=()=>nativeFetch(input,init).then(resolve)});
            }
            return nativeFetch(input,init);
          };
        }
        """
    )
    page.locator("#prompt").fill("绝不能发给旧 Session")
    page.locator("#newSession").click()
    page.wait_for_function("typeof window.__finishSlowCreate === 'function'")
    assert page.locator("#prompt").is_disabled()
    assert page.locator(".model-trigger").is_disabled()
    assert page.locator("#effortPicker").is_disabled()
    assert page.get_by_role("button", name="添加附件").is_disabled()
    assert page.locator("#sendButton").is_disabled()
    page.locator("#sendButton").evaluate("button => button.click()")
    page.locator("#prompt").evaluate(
        "textarea => textarea.dispatchEvent(new KeyboardEvent('keydown',{key:'Enter',bubbles:true}))"
    )
    assert page.evaluate("window.__sentDuringCreate.length") == 0
    page.evaluate("window.__finishSlowCreate()")
    page.wait_for_function("old => localStorage.getItem('oca.active-session') !== old", arg=old_id)
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    assert page.locator("#prompt").is_enabled()
    assert page.locator("#prompt").input_value() == "绝不能发给旧 Session"
    assert page.evaluate("window.__sentDuringCreate.length") == 0


def test_two_turn_history_replay_is_exact_idempotent_and_session_isolated(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    history_control: dict[str, list[dict[str, object]]] = {"events": []}

    def route_history(route) -> None:
        session_id = route.request.url.split("/v1/sessions/", 1)[1].split("/history", 1)[0]
        events = [event for event in history_control["events"] if event["session_id"] == session_id]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session_id": session_id, "events": events}),
        )

    page.route("**/v1/sessions/*/history", route_history)
    page.add_init_script(
        """
        window.__historyEvents=[];
        window.__turnCounts={};
        class HistorySocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(event){window.__historyEvents.push(event);this.onmessage?.({data:JSON.stringify(event)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;
              return setTimeout(()=>{
                this.onmessage?.({data:JSON.stringify({type:'sync_begin',session_id:this.sid})});
                const replay=JSON.parse(localStorage.getItem('__historyReplay')||'[]').filter(event=>event.session_id===this.sid);
                replay.forEach(event=>this.onmessage?.({data:JSON.stringify(event)}));
                this.onmessage?.({data:JSON.stringify({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0})});
              },0)
            }
            if(value.type!=='message') return;
            const count=(window.__turnCounts[this.sid]||0)+1;window.__turnCounts[this.sid]=count;
            const turn=`history-${this.sid}-${count}`,base=Date.parse(`2026-08-11T00:00:0${count}.000Z`),at=offset=>new Date(base+offset).toISOString();
            window.__historyEvents.push({type:'user_message',session_id:this.sid,turn_id:turn,sequence:0,at:at(0),content:value.content});
            const events=[
              {type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at:at(100)},
              {type:'progress',session_id:this.sid,turn_id:turn,sequence:2,at:at(200),phase:'analysis',status:'started',message:`分析 ${count}`,tool_use_id:`analysis-${count}`},
              {type:'progress',session_id:this.sid,turn_id:turn,sequence:3,at:at(300),phase:'analysis',status:'completed',message:`分析 ${count}`,tool_use_id:`analysis-${count}`},
              {type:'delta',session_id:this.sid,turn_id:turn,sequence:4,at:at(400),content:`说明 ${count}`},
              {type:'progress',session_id:this.sid,turn_id:turn,sequence:5,at:at(500),phase:count===1?'tool':'task',status:'started',message:count===1?'Write':'Bash',tool_name:count===1?'Write':'Bash',tool_use_id:`tool-${count}`,task_id:`task-${count}`},
              {type:'progress',session_id:this.sid,turn_id:turn,sequence:6,at:at(1200),phase:count===1?'tool':'task',status:'completed',message:count===1?'Write 完成':'Bash 完成',tool_name:count===1?'Write':'Bash',tool_use_id:`tool-${count}`,task_id:`task-${count}`},
              {type:'delta',session_id:this.sid,turn_id:turn,sequence:7,at:at(1500),content:`## 最终 ${count}`},
              {type:'done',session_id:this.sid,turn_id:turn,sequence:8,at:at(2100),completed:true,stop_reason:'stop',usage:{input_tokens:count,output_tokens:count+1}},
            ];
            events.forEach(event=>this.emit(event));
          }
          close(){this.readyState=3}
        }
        window.WebSocket=HistorySocket;
        """
    )
    _connect_and_create(page, web_server)
    session_a = page.evaluate("localStorage.getItem('oca.active-session')")
    for prompt in ("A 第一轮", "A 第二轮"):
        page.locator("#prompt").fill(prompt)
        page.locator("#prompt").press("Enter")
        page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)
    assert page.locator(".message.user").count() == 2
    assert page.locator(".message.assistant").count() == 2
    assert page.locator(".task-card").count() == 2

    page.locator("#newSession").click()
    page.wait_for_function(
        "old => localStorage.getItem('oca.active-session') !== old", arg=session_a
    )
    session_b = page.evaluate("localStorage.getItem('oca.active-session')")
    page.locator("#connectionBadge b").get_by_text("已连接", exact=True).wait_for(timeout=5_000)
    page.locator("#prompt").fill("B 独立轮次")
    page.locator("#prompt").press("Enter")
    page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)
    assert page.locator("#messages").get_by_text("A 第一轮", exact=True).count() == 0
    history_control["events"] = page.evaluate("window.__historyEvents")
    page.evaluate(
        "events => localStorage.setItem('__historyReplay', JSON.stringify(events))",
        history_control["events"],
    )
    page.locator(f'.session-row[data-session-id="{session_a}"]').click()
    page.locator("#messages").get_by_text("A 第一轮", exact=True).wait_for(timeout=5_000)

    def snapshot() -> list[dict[str, object]]:
        return page.locator("#messages .message").evaluate_all(
            """
            articles => articles.map(article => ({
              role:article.classList.contains('user')?'user':'assistant',
              content:article.querySelector('.message-text')?.textContent||'',
              task:article.querySelector('.task-card') ? {
                status:[...article.querySelector('.task-card').classList].filter(value=>value!=='task-card')[0],
                heading:article.querySelector('.task-card > header strong')?.textContent,
                rows:[...article.querySelector('.steps').children].map(row=>({
                  kind:row.dataset.kind||'step',
                  text:row.textContent,
                  activities:row.querySelector('.step-row button')?.textContent||'',
                })),
              } : null,
            }))
            """
        )

    before = snapshot()
    assert [item["role"] for item in before] == ["user", "assistant", "user", "assistant"]
    assert all(before[index]["task"] is not None for index in (1, 3))
    assert all("活动 2" in str(before[index]["task"]) for index in (1, 3))
    assert page.evaluate(
        "[...document.querySelectorAll('.message.assistant')].every(article => article.querySelector('.task-card').compareDocumentPosition(article.querySelector('.message-text')) & Node.DOCUMENT_POSITION_FOLLOWING)"
    )
    page.reload(wait_until="domcontentloaded")
    page.locator("#messages").get_by_text("A 第一轮", exact=True).wait_for(timeout=10_000)
    after = snapshot()
    assert after == before
    assert page.locator(".message.user").count() == 2
    assert page.locator(".task-card").count() == 2
    page.reload(wait_until="domcontentloaded")
    page.locator("#messages").get_by_text("A 第一轮", exact=True).wait_for(timeout=10_000)
    assert snapshot() == before
    assert page.locator(f'.session-row[data-session-id="{session_b}"]').count() == 1
    page.locator(f'.session-row[data-session-id="{session_b}"]').click()
    page.locator("#messages").get_by_text("B 独立轮次", exact=True).wait_for(timeout=5_000)
    assert page.locator("#messages").get_by_text("A 第一轮", exact=True).count() == 0
    assert page.locator(".message.user").count() == 1
    assert page.locator(".task-card").count() == 1


def test_history_keeps_a_task_anchor_for_a_turn_without_any_delta(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    persisted: dict[str, list[dict[str, object]]] = {"events": []}

    def route_history(route) -> None:
        session_id = route.request.url.split("/v1/sessions/", 1)[1].split("/history", 1)[0]
        events = [event for event in persisted["events"] if event["session_id"] == session_id]
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"session_id": session_id, "events": events}),
        )

    page.route("**/v1/sessions/*/history", route_history)
    page.add_init_script(
        """
        window.__anchorHistory=[];
        class AnchorSocket {
          constructor(){this.readyState=0;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          deliver(event){window.__anchorHistory.push(event);this.onmessage?.({data:JSON.stringify(event)})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){
              this.sid=value.session_id;
              return setTimeout(()=>{
                this.onmessage?.({data:JSON.stringify({type:'sync_begin',session_id:this.sid})});
                const replay=JSON.parse(localStorage.getItem('__anchorReplay')||'[]').filter(event=>event.session_id===this.sid);
                replay.forEach(event=>this.onmessage?.({data:JSON.stringify(event)}));
                this.onmessage?.({data:JSON.stringify({type:'ready',session_id:this.sid,task_state:'idle',turn_id:null,last_sequence:0})});
              },0)
            }
            if(value.type!=='message') return;
            const count=(this.count||0)+1;this.count=count;
            const turn=`anchor-${count}`,base=Date.parse(`2026-08-11T01:00:0${count}.000Z`),at=offset=>new Date(base+offset).toISOString();
            window.__anchorHistory.push({type:'user_message',session_id:this.sid,turn_id:turn,sequence:0,at:at(0),content:value.content});
            this.deliver({type:'turn_started',session_id:this.sid,turn_id:turn,sequence:1,at:at(100)});
            this.deliver({type:'progress',session_id:this.sid,turn_id:turn,sequence:2,at:at(200),phase:'tool',status:'started',message:count===1?'等待取消':'Write',tool_name:count===1?'Bash':'Write',tool_use_id:`anchor-tool-${count}`});
            this.deliver({type:'progress',session_id:this.sid,turn_id:turn,sequence:3,at:at(500),phase:'tool',status:count===1?'failed':'completed',message:count===1?'已中止':'Write 完成',tool_name:count===1?'Bash':'Write',tool_use_id:`anchor-tool-${count}`});
            if(count===1)this.deliver({type:'done',session_id:this.sid,turn_id:turn,sequence:4,at:at(800),completed:false,stop_reason:'stopped',usage:null});
            else {
              this.deliver({type:'delta',session_id:this.sid,turn_id:turn,sequence:4,at:at(700),content:'## 第二轮最终答案'});
              this.deliver({type:'done',session_id:this.sid,turn_id:turn,sequence:5,at:at(900),completed:true,stop_reason:'stop',usage:null});
            }
          }
          close(){this.readyState=3}
        }
        window.WebSocket=AnchorSocket;
        """
    )
    _connect_and_create(page, web_server)
    for prompt in ("第一轮无文本", "第二轮有答案"):
        page.locator("#prompt").fill(prompt)
        page.locator("#prompt").press("Enter")
        page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)

    def snapshot() -> list[dict[str, object]]:
        return page.locator("#messages .message").evaluate_all(
            """
            articles => articles.map(article => ({
              role:article.classList.contains('user')?'user':'assistant',
              turn:article.classList.contains('user')?'':(article.dataset.turnId||''),
              text:article.querySelector('.message-text')?.textContent||'',
              card:article.querySelector('.task-card')?.textContent||'',
            }))
            """
        )

    before = snapshot()
    assert [item["role"] for item in before] == ["user", "assistant", "user", "assistant"]
    assert "已停止" in str(before[1]["card"])
    assert before[1]["text"] == ""
    assert "任务完成" in str(before[3]["card"])
    assert "第二轮最终答案" in str(before[3]["text"])
    assert page.locator(".task-card").count() == 2
    assert page.locator('.message[data-turn-id="anchor-1"] .task-card').count() == 1
    assert page.locator('.message[data-turn-id="anchor-2"] .task-card').count() == 1
    assert page.evaluate(
        """
        () => {
          const article=document.querySelector('.message[data-turn-id="anchor-2"]');
          return Boolean(article.querySelector('.task-card').compareDocumentPosition(article.querySelector('.message-text')) & Node.DOCUMENT_POSITION_FOLLOWING);
        }
        """
    )

    persisted["events"] = page.evaluate("window.__anchorHistory")
    page.evaluate(
        "events => localStorage.setItem('__anchorReplay', JSON.stringify(events))",
        persisted["events"],
    )
    page.reload(wait_until="domcontentloaded")
    page.get_by_text("第二轮最终答案", exact=True).wait_for(timeout=10_000)
    assert snapshot() == before
    assert page.locator(".task-card").count() == 2


def test_streaming_deltas_follow_only_while_the_user_stays_at_the_bottom(
    web_server: dict[str, str], browser_page: Page
) -> None:
    page = browser_page
    page.add_init_script(
        """
        class StreamingSocket {
          constructor(){this.readyState=0;this.sequence=1;window.__streamingSocket=this;setTimeout(()=>{this.readyState=1;this.onopen?.({})},0)}
          emit(event){this.onmessage?.({data:JSON.stringify(event)})}
          delta(content){this.emit({type:'delta',session_id:this.sid,turn_id:'scroll-turn',sequence:++this.sequence,at:new Date().toISOString(),content})}
          send(raw){
            const value=JSON.parse(raw);
            if(value.type==='hello'){this.sid=value.session_id;return setTimeout(()=>this.emit({type:'ready',session_id:this.sid,task_state:'idle'}),0)}
            if(value.type!=='message') return;
            this.emit({type:'turn_started',session_id:this.sid,turn_id:'scroll-turn',sequence:1,at:new Date().toISOString()});
            this.delta(Array.from({length:80},(_,index)=>`初始行 ${index}\\n\\n`).join(''));
          }
          close(){this.readyState=3}
        }
        window.WebSocket=StreamingSocket;
        window.__emitStreamingDelta=content=>window.__streamingSocket.delta(content);
        window.__finishStreaming=()=>window.__streamingSocket.emit({type:'done',session_id:window.__streamingSocket.sid,turn_id:'scroll-turn',sequence:++window.__streamingSocket.sequence,at:new Date().toISOString(),completed:true,stop_reason:'stop',usage:null});
        """
    )
    _connect_and_create(page, web_server)
    page.locator("#prompt").fill("检查流式贴底")
    page.locator("#prompt").press("Enter")
    page.wait_for_function(
        """
        () => {
          const node=document.querySelector('#messages');
          return node.scrollHeight>node.clientHeight && node.scrollHeight-node.scrollTop-node.clientHeight<=2;
        }
        """,
        timeout=5_000,
    )
    scroll_top_before = page.locator("#messages").evaluate("node => node.scrollTop")
    page.evaluate(
        "window.__emitStreamingDelta(Array.from({length:12},(_,index)=>`贴底追加 ${index}\\n\\n`).join(''))"
    )
    page.get_by_text("贴底追加 11", exact=True).wait_for(timeout=5_000)
    page.wait_for_function(
        """
        () => {const node=document.querySelector('#messages');return node.scrollHeight-node.scrollTop-node.clientHeight<=2}
        """
    )
    assert page.locator("#messages").evaluate("node => node.scrollTop") > scroll_top_before
    assert page.evaluate(
        """
        () => {const node=document.querySelector('#messages');return node.scrollHeight-node.scrollTop-node.clientHeight<=2}
        """
    )

    page.locator("#messages").evaluate(
        "node => {node.scrollTop=0;node.dispatchEvent(new Event('scroll',{bubbles:true}))}"
    )
    page.wait_for_timeout(50)
    page.evaluate(
        "window.__emitStreamingDelta(Array.from({length:12},(_,index)=>`手动上滚后追加 ${index}\\n\\n`).join(''))"
    )
    page.get_by_text("手动上滚后追加 11", exact=True).wait_for(timeout=5_000)
    page.wait_for_timeout(100)
    assert page.locator("#messages").evaluate("node => node.scrollTop") == 0
    assert page.locator("#messages").evaluate("node => node.scrollTop") == 0
    page.evaluate("window.__finishStreaming()")
    page.get_by_role("button", name="发送", exact=True).wait_for(timeout=5_000)


def test_webkit_core_layout_and_model_keyboard_smoke(web_server: dict[str, str]) -> None:
    """Exercise the Safari-nearest engine when its Linux host libraries are present."""
    with sync_playwright() as playwright:
        try:
            browser = playwright.webkit.launch()
        except Exception as exc:
            text = str(exc)
            if "missing dependencies" in text or "error while loading shared libraries" in text:
                pytest.skip(
                    f"Playwright WebKit host dependencies unavailable: {text.splitlines()[-1]}"
                )
            raise
        context = browser.new_context(viewport={"width": 820, "height": 620})
        page = context.new_page()
        errors: list[str] = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        try:
            _connect_and_create(page, web_server)
            trigger = page.locator(".model-trigger")
            trigger.focus()
            trigger.press("ArrowDown")
            assert page.get_by_role("listbox", name="选择模型").is_visible()
            trigger.press("Escape")
            assert page.get_by_role("listbox", name="选择模型").is_hidden()
            page.get_by_role("button", name="打开沙箱文件").click()
            assert page.get_by_role("button", name="关闭文件面板").is_visible()
            composer = page.locator("#composer").bounding_box()
            assert composer and composer["y"] + composer["height"] <= 620
            assert errors == []
        finally:
            context.close()
            browser.close()
