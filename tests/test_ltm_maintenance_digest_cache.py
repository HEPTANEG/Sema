import json

from memory_engine.ltm import maintenance_digest_cache


def test_pending_digest_write_overwrites_current_draft(isolated_workspace):
    first = maintenance_digest_cache.write_pending_digest(
        "run-1",
        "## Recent Work\nfirst\n\n## Next Maintenance Hints\nnone",
        root=isolated_workspace,
    )
    second = maintenance_digest_cache.write_pending_digest(
        "run-1",
        "## Recent Work\nsecond\n\n## Next Maintenance Hints\nnone",
        references={"object_ids": ["mem_" + "a" * 32]},
        root=isolated_workspace,
    )

    pending = maintenance_digest_cache.load_pending_digest(isolated_workspace)

    assert first["status"] == "ok"
    assert second["status"] == "ok"
    assert pending["content"].startswith("## Recent Work\nsecond")
    assert pending["references"]["object_ids"] == ["mem_" + "a" * 32]


def test_pending_digest_rejects_over_token_limit(isolated_workspace):
    result = maintenance_digest_cache.write_pending_digest(
        "run-1",
        "token " * 2000,
        root=isolated_workspace,
    )

    assert result["status"] == "rejected"
    assert result["error"]["code"] == "digest_too_large"
    assert maintenance_digest_cache.load_pending_digest(isolated_workspace) is None


def test_completed_digest_append_trims_latest_five(isolated_workspace):
    for index in range(7):
        run_id = f"run-{index}"
        assert maintenance_digest_cache.write_pending_digest(
            run_id,
            f"## Recent Work\nrun {index}\n\n## Next Maintenance Hints\nnone",
            root=isolated_workspace,
        )["status"] == "ok"
        assert maintenance_digest_cache.persist_pending_digest(run_id, isolated_workspace)["status"] == "written"

    rows = [
        json.loads(line)
        for line in maintenance_digest_cache.completed_digests_path(isolated_workspace).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    assert len(rows) == 5
    assert rows[0]["maintenance_run_id"] == "run-6"
    assert rows[-1]["maintenance_run_id"] == "run-2"
    assert maintenance_digest_cache.load_pending_digest(isolated_workspace) is None


def test_recent_digest_loading_uses_dynamic_injection_count(isolated_workspace):
    for index in range(5):
        run_id = f"run-{index}"
        maintenance_digest_cache.write_pending_digest(
            run_id,
            f"## Recent Work\nrun {index}\n\n## Next Maintenance Hints\nnone",
            root=isolated_workspace,
        )
        maintenance_digest_cache.persist_pending_digest(run_id, isolated_workspace)

    assert maintenance_digest_cache.single_digest_max_tokens(100) == 500
    assert maintenance_digest_cache.injected_digest_count(200) == 3
    assert maintenance_digest_cache.injected_digest_count(1000) == 1
    assert len(maintenance_digest_cache.load_recent_digests(isolated_workspace, pressure_threshold_slots=200)) == 3
    assert len(maintenance_digest_cache.load_recent_digests(isolated_workspace, pressure_threshold_slots=1000)) == 1


def test_corrupt_completed_digest_cache_is_ignored(isolated_workspace):
    path = maintenance_digest_cache.completed_digests_path(isolated_workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json}\n", encoding="utf-8")

    assert maintenance_digest_cache.load_recent_digests(isolated_workspace) == []
