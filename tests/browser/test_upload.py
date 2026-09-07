from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

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
    WebDriverWait(driver, 20).until(
        lambda current: any(
            item.text.splitlines()[0] == name
            for item in current.find_elements(By.CSS_SELECTOR, "#document-list .document-item")
        )
    )


@pytest.fixture
def server(tmp_path: Path):
    port = _free_port()
    data_dir = tmp_path / "data"
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
    try:
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
            raise AssertionError(f"EasyLearn 服务未就绪：{output}")
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
    source = ROOT / "docs" / "FastAPI_MinerU方案与交互示例_v2" / "FastAPI_MinerU最终交付页面_v2.png"
    destination = tmp_path / "browser-upload.png"
    shutil.copyfile(source, destination)
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
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    source = ROOT / "docs" / "FastAPI_MinerU方案与交互示例_v2" / "FastAPI_MinerU最终交付页面_v2.png"
    shutil.copyfile(source, first)
    shutil.copyfile(source, second)
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

    item = browser.find_element(By.CSS_SELECTOR, "#document-list .document-item")
    document_id = item.get_attribute("data-document-id")
    favorite = item.find_element(By.CSS_SELECTOR, ".favorite-action")
    favorite.click()
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
    delete.click()
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
