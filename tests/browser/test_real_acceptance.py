from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import Select, WebDriverWait

if os.environ.get("EASYLEARN_RUN_BROWSER_TESTS") != "1":
    pytest.skip(
        "设置 EASYLEARN_RUN_BROWSER_TESTS=1 后运行 Selenium 浏览器测试",
        allow_module_level=True,
    )

BASE_URL = os.environ.get("EASYLEARN_ACCEPTANCE_BASE_URL", "http://127.0.0.1:8765")
PAPER = Path(
    os.environ.get(
        "EASYLEARN_ACCEPTANCE_PDF",
        r"F:\Papers\HOW DO VISION TRANSFORMERS WORK.pdf",
    )
)
pytestmark = pytest.mark.browser


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
    driver_path = os.environ.get("EASYLEARN_CHROMEDRIVER")
    service = Service(executable_path=driver_path) if driver_path else Service()
    try:
        driver = webdriver.Chrome(service=service, options=options)
    except WebDriverException as exc:
        pytest.fail(f"ChromeDriver 启动失败：{exc}")
    try:
        driver.set_page_load_timeout(30)
        yield driver
    finally:
        driver.quit()


def test_real_paper_covers_parse_progress_and_pdf_controls(browser):
    assert PAPER.is_file(), f"真实验收论文不存在：{PAPER}"
    browser.get(BASE_URL)
    WebDriverWait(browser, 30).until(
        lambda current: current.find_element(By.ID, "empty-upload-state").is_displayed()
    )

    existing_document_ids = {
        item.get_attribute("data-document-id")
        for item in browser.find_elements(By.CSS_SELECTOR, "#document-list .document-item")
    }
    browser.find_element(By.ID, "file-input").send_keys(str(PAPER))

    def uploaded_document_id(current: webdriver.Chrome) -> str | bool:
        try:
            for item in current.find_elements(By.CSS_SELECTOR, "#document-list .document-item"):
                document_id = item.get_attribute("data-document-id")
                name = item.find_element(By.CSS_SELECTOR, ".document-item-name").text
                if document_id not in existing_document_ids and name == PAPER.name:
                    return document_id
        except StaleElementReferenceException:
            return False
        return False

    document_id = WebDriverWait(browser, 60).until(uploaded_document_id)

    def fetch_json(current: webdriver.Chrome, path: str) -> dict:
        return current.execute_async_script(
            """
            const [path, done] = arguments;
            fetch(path)
              .then(async (res) => done({status: res.status, payload: await res.json()}))
              .catch((error) => done({error: String(error)}));
            """,
            path,
        )["payload"]

    def parse_task(current: webdriver.Chrome) -> dict | bool:
        document = fetch_json(current, f"/api/documents/{document_id}")
        tasks = [task for task in document.get("tasks", []) if task.get("kind") == "parse"]
        return tasks[-1] if tasks else False

    task = WebDriverWait(browser, 60).until(parse_task)
    task_id = task["task_id"]

    progress_seen = False

    def observe_progress(current: webdriver.Chrome) -> bool:
        nonlocal progress_seen
        panel = current.find_element(By.ID, "parse-progress")
        progress_seen = progress_seen or panel.is_displayed()
        return progress_seen

    WebDriverWait(browser, 120).until(observe_progress)

    def result_ready(current: webdriver.Chrome) -> bool:
        try:
            task = fetch_json(current, f"/api/tasks/{task_id}")
            if task.get("status") != "succeeded":
                return False
            statuses = current.find_elements(By.CSS_SELECTOR, "#task-list .task-item")
            pages = current.find_elements(By.CSS_SELECTOR, "#pdf-pages .pdf-page canvas")
            counter_elem = current.find_element(By.ID, "page-count")
            page_counter = counter_elem.get_attribute("textContent").strip()
        except StaleElementReferenceException:
            return False
        return (
            bool(statuses)
            and bool(pages)
            and re.fullmatch(r"\d+\s*/\s*\d+", page_counter) is not None
        )

    WebDriverWait(browser, 1200).until(result_ready)
    assert browser.find_element(By.ID, "empty-upload-state").get_attribute("hidden") == "true"
    assert browser.find_element(By.ID, "parse-progress").get_attribute("hidden") == "true"

    page_count = browser.find_element(By.ID, "page-count").get_attribute("textContent").strip()
    total_pages = int(page_count.split("/")[1].strip())
    assert total_pages >= 2
    next_page = browser.find_element(By.ID, "next-page")
    assert next_page.is_enabled()
    browser.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
        next_page,
    )
    next_page.click()
    WebDriverWait(browser, 30).until(
        lambda current: current.find_element(By.ID, "page-count")
        .get_attribute("textContent").strip().startswith("2 /")
    )
    prev_page = browser.find_element(By.ID, "prev-page")
    browser.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
        prev_page,
    )
    prev_page.click()
    WebDriverWait(browser, 30).until(
        lambda current: current.find_element(By.ID, "page-count")
        .get_attribute("textContent").strip().startswith("1 /")
    )

    zoom_element = browser.find_element(By.ID, "zoom-select")
    zoom = Select(zoom_element)
    browser.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
        zoom_element,
    )
    zoom.select_by_value("1.5")
    WebDriverWait(browser, 30).until(
        lambda current: float(
            current.find_element(By.ID, "pdf-viewer").get_attribute("data-scale")
        ) == 1.5
    )
    reset_zoom = browser.find_element(By.ID, "reset-zoom")
    browser.execute_script(
        "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
        reset_zoom,
    )
    reset_zoom.click()
    WebDriverWait(browser, 30).until(
        lambda current: float(
            current.find_element(By.ID, "pdf-viewer").get_attribute("data-scale")
        ) == 1
    )
