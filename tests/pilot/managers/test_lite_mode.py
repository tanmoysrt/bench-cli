"""Tests for lite mode: one process serving web, realtime and jobs."""

from __future__ import annotations

import json
from pathlib import Path

from pilot.config import (
    AppConfig,
    BenchConfig,
    LiteModeConfig,
    MariaDBConfig,
    RedisConfig,
    WorkerConfig,
    WorkerGroup,
)
from pilot.core.bench import Bench
from pilot.managers.processes.definitions import ProcessDefinitionBuilder


def make_bench(tmp_path: Path, lite_mode: LiteModeConfig | None = None) -> Bench:
    config = BenchConfig(
        name="test-bench",
        python_version="3.14",
        apps=[AppConfig(name="frappe", repo="https://github.com/frappe/frappe", branch="version-16")],
        mariadb=MariaDBConfig(root_password="root"),
        redis=RedisConfig(cache_port=13000, queue_port=11000),
        workers=WorkerConfig(
            groups=[
                WorkerGroup(queues=["default", "short"], count=2),
                WorkerGroup(queues=["long", "default"], count=1),
            ]
        ),
        lite_mode=lite_mode or LiteModeConfig(enabled=True),
    )
    runner = tmp_path / "apps" / "frappe" / "frappe" / "runner.py"
    runner.parent.mkdir(parents=True, exist_ok=True)
    runner.touch()
    return Bench(config, tmp_path)


def _definitions(bench: Bench) -> ProcessDefinitionBuilder:
    return ProcessDefinitionBuilder(bench, bench.env_path / "bin" / "python", False)


def _web(bench: Bench, dev: bool = False):
    return _definitions(bench).web_definition(dev=dev)


def _argument(bench: Bench, flag: str) -> str:
    return next(arg.split("=", 1)[1] for arg in _web(bench).argv if arg.startswith(f"{flag}="))


def test_lite_replaces_the_whole_process_set(tmp_path: Path) -> None:
    names = {pd.name for pd in _definitions(make_bench(tmp_path)).prod_process_definitions()}

    assert names == {"web", "admin", "redis_cache", "redis_queue"}


def test_lite_runs_the_frappe_runner(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)

    web = _web(bench)

    assert web.argv[0].endswith("bin/python")
    assert web.argv[1:3] == ["-m", "frappe.runner"]
    assert web.working_dir == bench.sites_path


def test_lite_passes_config_through_as_flags(tmp_path: Path) -> None:
    bench = make_bench(
        tmp_path,
        LiteModeConfig(
            enabled=True,
            restart_after_requests=4000,
            restart_after_jobs=900,
            restart_idle_seconds=120,
            request_drain_seconds=45,
            job_drain_seconds=500,
        ),
    )

    assert _argument(bench, "--port") == str(bench.config.http_port)
    assert _argument(bench, "--restart-after-requests") == "4000"
    assert _argument(bench, "--restart-after-jobs") == "900"
    assert _argument(bench, "--restart-idle-seconds") == "120"
    assert _argument(bench, "--request-drain-seconds") == "45"
    assert _argument(bench, "--job-drain-seconds") == "500"


def test_lite_serves_the_union_of_worker_queues(tmp_path: Path) -> None:
    # One process runs every queue the groups list; counts do not apply.
    assert _argument(make_bench(tmp_path), "--queue") == "default,short,long"


def test_lite_job_threads_total_the_worker_counts(tmp_path: Path) -> None:
    # The groups carry 2 + 1; lite runs one pool sized to the total.
    assert _argument(make_bench(tmp_path), "--job-threads") == "3"


def test_lite_embeds_realtime_on_the_web_port(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)

    bench.sites_path.mkdir(parents=True, exist_ok=True)
    bench.write_common_site_config()
    common = json.loads((bench.sites_path / "common_site_config.json").read_text())

    assert bench.realtime_port == bench.config.http_port
    assert common["socketio_port"] == bench.config.http_port


def test_a_frappe_without_the_runner_keeps_the_ordinary_set(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)
    (tmp_path / "apps" / "frappe" / "frappe" / "runner.py").unlink()

    assert bench.is_lite_mode is False
    assert bench.realtime_port == bench.config.socketio_port
    assert "socketio" in {pd.name for pd in _definitions(bench).prod_process_definitions()}


def test_enforcing_the_rules_reverts_lite_mode_without_a_runner(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)
    (tmp_path / "apps" / "frappe" / "frappe" / "runner.py").unlink()
    audited: list[tuple[str, dict]] = []
    bench.audit_action = lambda category, fields: audited.append((category, fields))

    assert bench.enforce_lite_mode_rules() is True
    assert bench.config.lite_mode.enabled is False
    assert audited[0][0] == "lite_mode_disabled"


def test_lite_mode_survives_a_single_worker_group(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)
    bench.config.workers.groups = [WorkerGroup(queues=["default", "short", "long"], count=4)]

    assert bench.enforce_lite_mode_rules() is False
    assert bench.config.lite_mode.enabled is True


def test_lite_mode_folds_multiple_worker_groups_into_one(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)
    audited: list[tuple[str, dict]] = []
    bench.audit_action = lambda category, fields: audited.append((category, fields))

    assert bench.enforce_lite_mode_rules() is True
    assert bench.config.lite_mode.enabled is True
    assert [(group.queues, group.count) for group in bench.config.workers.groups] == [
        (["default", "short", "long"], 3)
    ]
    assert audited[0][0] == "worker_groups_collapsed"
    assert audited[0][1]["groups"] == 2


def test_enforcing_the_rules_leaves_lite_mode_off_alone(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, LiteModeConfig())

    assert bench.enforce_lite_mode_rules() is False


def test_lite_stop_timeout_covers_both_drains(tmp_path: Path) -> None:
    bench = make_bench(
        tmp_path, LiteModeConfig(enabled=True, request_drain_seconds=60, job_drain_seconds=600)
    )

    assert _web(bench).stop_timeout == 690


def test_lite_serves_assets_only_in_dev(tmp_path: Path) -> None:
    bench = make_bench(tmp_path)

    assert "--serve-assets" not in _web(bench).argv
    assert "--serve-assets" in _web(bench, dev=True).argv


def test_lite_dev_keeps_the_single_process(tmp_path: Path) -> None:
    # Without lite, dev swaps gunicorn for `frappe serve`; lite has no such split,
    # and dropping to the dev server would leave jobs and realtime unserved.
    names = {pd.name for pd in _definitions(make_bench(tmp_path)).process_definitions()}

    assert "worker_default_1" not in names
    assert "socketio" not in names


def test_lite_off_keeps_the_ordinary_process_set(tmp_path: Path) -> None:
    bench = make_bench(tmp_path, LiteModeConfig())

    names = {pd.name for pd in _definitions(bench).prod_process_definitions()}

    assert "socketio" in names
    assert "worker_default_short_1" in names
    assert "frappe.runner" not in _web(bench).argv
