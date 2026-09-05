import os
import subprocess
import sys
from pathlib import Path


def test_misspelled_startup_option_is_rejected_before_starting_python():
    script = Path(__file__).resolve().parents[2] / "deploy" / "run.ps1"
    result = subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-File",
            str(script),
            "-Action",
            "migrate",
            "-Python",
            sys.executable,
            "-ConfigPah",
            "missing.yaml",
        ],
        env=dict(os.environ, EASYLEARN_DATABASE_URL="invalid-test-url"),
        capture_output=True,
        text=True,
        timeout=10,
        creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
    )
    assert result.returncode != 0
    assert "ConfigPah" in result.stderr
    assert "Traceback" not in result.stderr
