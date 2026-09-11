from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen
from uuid import uuid4

import pytest

if os.environ.get("EASYLEARN_RUN_BROWSER_TESTS") != "1":
    pytest.skip(
        "设置 EASYLEARN_RUN_BROWSER_TESTS=1 后运行 Selenium 浏览器测试",
        allow_module_level=True,
    )

from selenium import webdriver  # noqa: E402
from selenium.common.exceptions import TimeoutException, WebDriverException  # noqa: E402
from selenium.webdriver.chrome.options import Options  # noqa: E402
from selenium.webdriver.chrome.service import Service  # noqa: E402
from selenium.webdriver.common.by import By  # noqa: E402
from selenium.webdriver.support.ui import WebDriverWait  # noqa: E402

pytestmark = pytest.mark.browser
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _performance_messages(driver: webdriver.Chrome) -> list[dict[str, object]]:
    return [json.loads(entry["message"])["message"] for entry in driver.get_log("performance")]


def _open_app(driver: webdriver.Chrome, base_url: str) -> list[dict[str, object]]:
    driver.get(base_url)
    observed: list[dict[str, object]] = []

    def page_booted(current: webdriver.Chrome) -> bool:
        observed.extend(_performance_messages(current))
        return any(
            message.get("method") == "Network.responseReceived"
            and message["params"]["response"]["url"].endswith("/api/documents")
            and message["params"]["response"]["status"] == 200
            for message in observed
        )

    try:
        WebDriverWait(driver, 10).until(page_booted)
    except TimeoutException as exc:
        raise AssertionError(
            "EasyLearn 页面未完成启动；浏览器控制台："
            + json.dumps(driver.get_log("browser"), ensure_ascii=False)
            + "；当前 URL："
            + driver.current_url
        ) from exc
    return observed


def _wait_for_document(driver: webdriver.Chrome, name: str) -> None:
    def predicate(current: webdriver.Chrome) -> bool:
        for item in current.find_elements(By.CSS_SELECTOR, "#document-list .document-item"):
            name_nodes = item.find_elements(By.CSS_SELECTOR, ".document-item-name")
            if name_nodes and name_nodes[0].text == name:
                return True
            lines = item.text.splitlines()
            if lines and (lines[0] == name or name in lines):
                return True
        return False

    WebDriverWait(driver, 20, ignored_exceptions=(WebDriverException,)).until(predicate)


def _start_server(tmp_path: Path, data_dir: Path) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    config = tmp_path / "config.toml"
    config.write_text(
        "[app]\n"
        'host = "127.0.0.1"\n'
        f"port = {port}\n"
        f'data_dir = "{data_dir.as_posix()}"\n'
        "open_browser = false\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    source_path = str(ROOT / "src")
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (source_path, environment.get("PYTHONPATH")) if part
    )
    process = subprocess.Popen(
        [sys.executable, "-m", "easylearn", "--config", str(config)],
        cwd=ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = process.stdout.read() if process.stdout else ""
            raise AssertionError(f"EasyLearn 服务提前退出：{output}")
        try:
            with urlopen(f"{base_url}/api/health", timeout=1) as response:
                if response.status == 200:
                    break
        except OSError:
            time.sleep(0.1)
    else:
        output = process.stdout.read() if process.stdout else ""
        process.kill()
        raise AssertionError(f"EasyLearn 服务未就绪：{output}")
    return process, base_url


@pytest.fixture
def server(tmp_path: Path):
    data_dir = tmp_path / "data"
    process, base_url = _start_server(tmp_path, data_dir)
    try:
        yield base_url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)


@pytest.fixture
def browser(tmp_path: Path):
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--window-size=1600,1000")
    options.add_argument(f"--user-data-dir={tmp_path / 'chrome-profile'}")
    options.add_argument("--no-first-run")
    options.add_argument("--disable-background-networking")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-gpu")
    options.add_argument("--disable-dev-shm-usage")
    options.set_capability("goog:loggingPrefs", {"browser": "ALL", "performance": "ALL"})
    driver_path = os.environ.get("EASYLEARN_CHROMEDRIVER")
    service = Service(executable_path=driver_path) if driver_path else Service()
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except WebDriverException as exc:
        pytest.fail(f"ChromeDriver 启动失败：{exc}")
    try:
        driver.set_page_load_timeout(20)
        yield driver
    finally:
        driver.quit()


@pytest.fixture
def image_file(tmp_path: Path) -> Path:
    from PIL import Image

    destination = tmp_path / "browser-upload.png"
    Image.new("RGB", (100, 100), color=(56, 96, 244)).save(destination, "PNG")
    return destination


def test_browser_modules_load_with_javascript_mime(server, browser):
    messages = _open_app(browser, server)

    responses = []
    for message in messages:
        if message.get("method") != "Network.responseReceived":
            continue
        response = message["params"]["response"]
        if response["url"].endswith("/static/pdfjs/pdf.min.mjs"):
            responses.append(response)
    assert responses and responses[-1]["status"] == 200
    assert responses[-1]["mimeType"] in {"text/javascript", "application/javascript"}
    assert not any(
        "Failed to load module script" in entry["message"]
        for entry in browser.get_log("browser")
    )


def test_uploading_image_posts_to_api_and_renders_document(server, browser, image_file):
    _open_app(browser, server)
    browser.find_element(By.ID, "file-input").send_keys(str(image_file))
    _wait_for_document(browser, image_file.name)

    requests = []
    responses = []
    for message in _performance_messages(browser):
        if message.get("method") == "Network.requestWillBeSent":
            request = message["params"]["request"]
            if request["url"].endswith("/api/documents"):
                requests.append(request)
        elif message.get("method") == "Network.responseReceived":
            response = message["params"]["response"]
            if response["url"].endswith("/api/documents"):
                responses.append(response)

    assert [request["method"] for request in requests if request["method"] == "POST"] == ["POST"]
    assert any(response["status"] == 201 for response in responses)
    assert browser.find_element(By.ID, "document-name").text == image_file.name


def test_uploading_two_images_keeps_both_documents_in_sidebar(server, browser, tmp_path):
    from PIL import Image

    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    Image.new("RGB", (100, 100), color=(56, 96, 244)).save(first, "PNG")
    Image.new("RGB", (100, 100), color=(16, 185, 129)).save(second, "PNG")
    _open_app(browser, server)

    for image in (first, second):
        browser.find_element(By.ID, "file-input").send_keys(str(image))
        _wait_for_document(browser, image.name)

    items = browser.find_elements(By.CSS_SELECTOR, "#document-list .document-item")
    assert {item.text.splitlines()[0] for item in items} == {first.name, second.name}


def test_empty_workspace_exposes_upload_dropzone_without_model_selector(server, browser):
    _open_app(browser, server)

    empty = browser.find_element(By.ID, "empty-upload-state")
    assert empty.is_displayed()
    assert "点击上传或者拖入文件开始解析" in empty.text
    assert not browser.find_element(By.ID, "model-select").is_displayed()


def test_document_workspace_exposes_actions_and_settings(server, browser, image_file):
    _open_app(browser, server)
    browser.find_element(By.ID, "file-input").send_keys(str(image_file))
    _wait_for_document(browser, image_file.name)

    assert not browser.find_element(By.ID, "empty-upload-state").is_displayed()

    for element_id in ("settings-button", "reparse-button", "download-button", "model-select"):
        assert browser.find_element(By.ID, element_id).is_displayed()

    settings = browser.find_element(By.ID, "settings-button")
    assert settings.get_attribute("aria-expanded") == "false"
    settings.click()
    WebDriverWait(browser, 10).until(
        lambda current: current.find_element(By.ID, "settings-panel").is_displayed()
    )
    assert settings.get_attribute("aria-expanded") == "true"


def test_document_actions_support_favorite_and_delete(server, browser, image_file):
    _open_app(browser, server)
    browser.find_element(By.ID, "file-input").send_keys(str(image_file))
    _wait_for_document(browser, image_file.name)

    document_id = WebDriverWait(browser, 10, ignored_exceptions=(WebDriverException,)).until(
        lambda current: current.find_element(
            By.CSS_SELECTOR, "#document-list .document-item"
        ).get_attribute("data-document-id")
    )
    favorite = WebDriverWait(browser, 10, ignored_exceptions=(WebDriverException,)).until(
        lambda current: current.find_element(
            By.CSS_SELECTOR,
            f'#document-list .document-item[data-document-id="{document_id}"] .favorite-action',
        )
    )
    browser.execute_script("arguments[0].click();", favorite)
    WebDriverWait(browser, 10).until(
        lambda current: current.find_element(
            By.CSS_SELECTOR,
            f'#document-list .document-item[data-document-id="{document_id}"] .favorite-action',
        ).get_attribute("aria-label") == "取消收藏"
    )

    delete = browser.find_element(
        By.CSS_SELECTOR,
        f'#document-list .document-item[data-document-id="{document_id}"] .delete-action',
    )
    browser.execute_script("arguments[0].click();", delete)
    dialog = browser.find_element(By.ID, "document-delete-dialog")
    WebDriverWait(browser, 10).until(lambda current: dialog.is_displayed())
    assert image_file.name in dialog.text
    browser.find_element(By.ID, "confirm-delete-button").click()
    WebDriverWait(browser, 10).until(
        lambda current: not current.find_elements(
            By.CSS_SELECTOR,
            f'#document-list .document-item[data-document-id="{document_id}"]',
        )
    )


def test_ctrl_wheel_changes_reader_scale(server, browser, image_file):
    _open_app(browser, server)
    browser.find_element(By.ID, "file-input").send_keys(str(image_file))
    _wait_for_document(browser, image_file.name)

    before = float(
        browser.execute_script(
            "return Number(document.getElementById('pdf-viewer').dataset.scale || '1');"
        )
    )
    browser.execute_script(
        "document.getElementById('pdf-viewer').dispatchEvent(new WheelEvent('wheel', "
        "{bubbles: true, cancelable: true, ctrlKey: true, deltaY: -100}));"
    )
    after = float(
        browser.execute_script(
            "return Number(document.getElementById('pdf-viewer').dataset.scale || '1');"
        )
    )
    assert after > before


def test_browser_smoke_settings_and_multi_node_edit(tmp_path: Path, browser):
    data_dir = tmp_path / "data"
    doc_id = uuid4()
    parse_id = uuid4()
    doc_dir = data_dir / "documents" / str(doc_id)
    parse_dir = doc_dir / "parses" / str(parse_id)
    parse_dir.mkdir(parents=True, exist_ok=True)

    from PIL import Image

    img_path = doc_dir / "original"
    Image.new("RGB", (100, 100), color=(56, 96, 244)).save(img_path, "PNG")
    preview_path = parse_dir / "preview.png"
    Image.new("RGB", (100, 100), color=(56, 96, 244)).save(preview_path, "PNG")

    from easylearn.database import SCHEMA_VERSION, _SCHEMA
    from easylearn.document_ir.schema import Block, DocumentIR, PageGeometry, TextNode

    ir = DocumentIR(
        document_id=doc_id,
        parse_run_id=parse_id,
        preview_asset_id=uuid4(),
        preview_sha256="a" * 64,
        mineru_version="test",
        adapter_version="test",
        pages=(
            PageGeometry(
                page_index=0,
                media_box=(0, 0, 100, 100),
                crop_box=(0, 0, 100, 100),
            ),
        ),
        blocks=(
            Block(
                block_id="block-multi",
                block_type="paragraph",
                order_index=0,
                source_nodes=(
                    TextNode(node_id="node-1", text="First phrase"),
                    TextNode(node_id="node-2", text="Second phrase"),
                ),
            ),
        ),
    )
    (parse_dir / "document.json").write_text(
        ir.model_dump_json(exclude_computed_fields=True), encoding="utf-8"
    )

    with sqlite3.connect(data_dir / "app.db") as conn:
        conn.executescript(_SCHEMA)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        conn.execute(
            "INSERT INTO documents (id, name, original_path, active_parse_id, favorite, created_at)"
            " VALUES (?, ?, ?, ?, 0, '2026-01-01T00:00:00+00:00')",
            (str(doc_id), "multi_doc.png", "original", str(parse_id)),
        )
        conn.execute(
            "INSERT INTO parse_results (id, document_id, preview_path, ir_path, raw_path, pages, metadata_json, created_at)"
            " VALUES (?, ?, ?, ?, NULL, 1, '{}', '2026-01-01T00:00:00+00:00')",
            (
                str(parse_id),
                str(doc_id),
                f"parses/{parse_id}/preview.png",
                f"parses/{parse_id}/document.json",
            ),
        )
        conn.commit()

    process, base_url = _start_server(tmp_path, data_dir)
    try:
        _open_app(browser, base_url)
        _wait_for_document(browser, "multi_doc.png")

        # 1. Verify dead phantom settings controls do NOT exist
        for phantom in ("cfg-header", "cfg-layout", "cfg-formula", "cfg-table"):
            assert (
                len(browser.find_elements(By.ID, phantom)) == 0
            ), f"Phantom control #{phantom} should not be present in DOM"

        # 2. Verify settings drawer can open, modify auto_translate, and apply
        settings_btn = browser.find_element(By.ID, "settings-button")
        settings_btn.click()
        WebDriverWait(browser, 10).until(
            lambda d: d.find_element(By.ID, "settings-panel").is_displayed()
        )
        auto_translate_cb = browser.find_element(By.ID, "auto-translate")
        browser.execute_script("arguments[0].click();", auto_translate_cb)
        apply_btn = browser.find_element(By.ID, "apply-settings-button")
        browser.execute_script("arguments[0].click();", apply_btn)
        WebDriverWait(browser, 10).until(
            lambda d: not d.find_element(By.ID, "settings-panel").is_displayed()
        )

        # 3. Click document and ensure multi-node block is rendered
        doc_item = browser.find_element(
            By.CSS_SELECTOR, f'#document-list .document-item[data-document-id="{doc_id}"]'
        )
        doc_item.click()

        WebDriverWait(browser, 10).until(
            lambda d: d.find_elements(
                By.CSS_SELECTOR, '.result-block[data-block-id="block-multi"]'
            )
        )
        block_el = browser.find_element(
            By.CSS_SELECTOR, '.result-block[data-block-id="block-multi"]'
        )
        assert "First phrase" in block_el.text
        assert "Second phrase" in block_el.text

        # 4. Click block to select it and show action bar, then click edit button
        block_el.click()
        edit_btn = block_el.find_element(By.CSS_SELECTOR, ".block-edit-button")
        WebDriverWait(browser, 10).until(lambda _: edit_btn.is_displayed())
        edit_btn.click()

        WebDriverWait(browser, 10).until(
            lambda d: d.find_elements(By.CSS_SELECTOR, ".block-edit-card")
        )
        edit_card = browser.find_element(By.CSS_SELECTOR, ".block-edit-card")
        nodes_container = edit_card.find_elements(
            By.CSS_SELECTOR, ".block-edit-nodes"
        )
        assert len(nodes_container) == 1, "Multi-node container should be present"

        textarea_1 = edit_card.find_element(
            By.CSS_SELECTOR, 'textarea[data-node-id="node-1"]'
        )
        textarea_2 = edit_card.find_element(
            By.CSS_SELECTOR, 'textarea[data-node-id="node-2"]'
        )
        assert textarea_1.get_attribute("value") == "First phrase"
        assert textarea_2.get_attribute("value") == "Second phrase"

        save_btn = edit_card.find_element(By.CSS_SELECTOR, ".block-edit-save-btn")

        # 5. Empty node text rejected
        textarea_1.clear()
        save_btn.click()
        WebDriverWait(browser, 10).until(
            lambda d: "不能为空" in d.find_element(By.ID, "toast").text
        )

        # 6. Save valid edit and verify update
        textarea_1.send_keys("Updated phrase")
        save_btn.click()

        WebDriverWait(browser, 10).until(
            lambda d: not d.find_elements(By.CSS_SELECTOR, ".block-edit-card")
        )

        block_after = browser.find_element(
            By.CSS_SELECTOR, '.result-block[data-block-id="block-multi"]'
        )
        assert "Updated phrase" in block_after.text
        assert "Second phrase" in block_after.text
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
