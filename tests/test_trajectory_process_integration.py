import json

from memory_engine.ltm import formation, formation_backfill, memorize
from persistence import trajectory


def test_automatic_formation_and_memorize_create_distinct_roots(monkeypatch, isolated_workspace):
    monkeypatch.setattr(formation, "_run_formation_maintenance_impl", lambda *_args, **_kwargs: {"status": "idle"})
    monkeypatch.setattr(formation_backfill, "_run_formation_backfill_impl", lambda *_args, **_kwargs: {"status": "completed"})
    monkeypatch.setattr(memorize, "_run_memorize_impl", lambda *_args, **_kwargs: {"status": "committed"})

    formation.run_formation_maintenance(runtime_state={})
    formation_backfill.run_formation_backfill(runtime_state={})
    memorize.run_memorize("remember this", runtime_state={})

    roots = trajectory.list_trajectories(limit=10)
    modes = {json.loads(root["metadata_json"]).get("formation_mode") or json.loads(root["metadata_json"]).get("memorize_mode") for root in roots}
    assert modes == {"online", "backfill", "memorize"}
    assert all(root["status"] == "completed" for root in roots)
