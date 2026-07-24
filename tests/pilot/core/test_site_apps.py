"""SiteApps.install_app/uninstall_app must clear the site's cache before and
after (success or failure).

Frappe's own install-app trusts an in-process module cache that isn't
refreshed after a newly fetched app lands in apps.txt, so a stale cache left
by an earlier frappe call against the site can make the new app's doctypes
silently fail to sync. See the incident this guards against: a fresh app's
after_install hook crashing because none of its doctypes were ever created.
Clearing after every install/uninstall attempt, success or failure, keeps
this site's own operations from becoming that stale cache for whatever runs
next.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from pilot.config import AppConfig, BenchConfig, MariaDBConfig, RedisConfig, SiteConfig, WorkerConfig
from pilot.core.app import App
from pilot.core.bench import Bench
from pilot.core.site import Site
from pilot.exceptions import CommandError


def make_bench(tmp_path: Path) -> Bench:
    config = BenchConfig(
        name="test-bench",
        python_version="3.14",
        apps=[AppConfig(name="frappe", repo="https://github.com/frappe/frappe", branch="version-16")],
        mariadb=MariaDBConfig(root_password="root"),
        redis=RedisConfig(cache_port=13000, queue_port=11000),
        workers=WorkerConfig(),
    )
    return Bench(config, tmp_path)


def make_site_and_app(tmp_path: Path) -> tuple[Site, App]:
    bench = make_bench(tmp_path)
    bench.create_directories()
    site = Site(SiteConfig(name="site1.localhost", apps=["frappe"]), bench)
    app = App(AppConfig(name="erpnext", repo="https://github.com/frappe/erpnext", branch="version-16"), bench)
    return site, app


def test_install_app_clears_cache_before_and_after_success(tmp_path: Path) -> None:
    site, app = make_site_and_app(tmp_path)

    with (
        patch("pilot.core.site.apps.run_command") as mock_rc,
        patch.object(Bench, "reload_workers"),
    ):
        site.install_app(app)

    commands = [call.args[0] for call in mock_rc.call_args_list]
    assert len(commands) == 3
    assert commands[0][-1] == "clear-cache"
    assert commands[1][-2:] == ["install-app", "erpnext"]
    assert commands[2][-1] == "clear-cache"


def test_install_app_clears_cache_after_failure(tmp_path: Path) -> None:
    site, app = make_site_and_app(tmp_path)

    def fail_on_install(argv, **kwargs):
        if "install-app" in argv:
            raise CommandError("boom")

    with (
        patch("pilot.core.site.apps.run_command", side_effect=fail_on_install) as mock_rc,
        patch.object(Bench, "reload_workers"),
        pytest.raises(CommandError),
    ):
        site.install_app(app)

    commands = [call.args[0] for call in mock_rc.call_args_list]
    assert len(commands) == 3
    assert commands[0][-1] == "clear-cache"
    assert commands[1][-2:] == ["install-app", "erpnext"]
    assert commands[2][-1] == "clear-cache"


def test_uninstall_app_clears_cache_after_success(tmp_path: Path) -> None:
    site, app = make_site_and_app(tmp_path)

    with (
        patch("pilot.core.site.apps.run_command") as mock_rc,
        patch.object(Bench, "reload_workers"),
    ):
        site.uninstall_app(app)

    commands = [call.args[0] for call in mock_rc.call_args_list]
    assert len(commands) == 2
    assert "uninstall-app" in commands[0]
    assert commands[1][-1] == "clear-cache"


def test_uninstall_app_clears_cache_after_failure(tmp_path: Path) -> None:
    site, app = make_site_and_app(tmp_path)

    def fail_on_uninstall(argv, **kwargs):
        raise CommandError("boom")

    with (
        patch("pilot.core.site.apps.run_command", side_effect=fail_on_uninstall) as mock_rc,
        patch.object(Bench, "reload_workers"),
        pytest.raises(CommandError),
    ):
        site.uninstall_app(app)

    commands = [call.args[0] for call in mock_rc.call_args_list]
    assert len(commands) == 2
    assert "uninstall-app" in commands[0]
    assert commands[1][-1] == "clear-cache"
