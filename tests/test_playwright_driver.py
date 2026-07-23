from pathlib import Path

import pytest

from effect_browser.browser import playwright as playwright_module
from effect_browser.browser.playwright import PlaywrightDriver


def test_failed_browser_launch_releases_playwright_runtime(
    monkeypatch, tmp_path: Path
) -> None:
    class FailingChromium:
        @staticmethod
        def launch(**_options):
            raise RuntimeError("synthetic launch failure")

    class FakePlaywright:
        chromium = FailingChromium()
        stopped = False

        def stop(self) -> None:
            self.stopped = True

    runtime = FakePlaywright()

    class FakeManager:
        @staticmethod
        def start():
            return runtime

    monkeypatch.setattr(playwright_module, "sync_playwright", FakeManager)

    with pytest.raises(RuntimeError, match="synthetic launch failure"):
        PlaywrightDriver(artifacts_directory=tmp_path / "artifacts")

    assert runtime.stopped is True
