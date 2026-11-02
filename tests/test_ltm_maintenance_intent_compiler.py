from memory_engine import event_stream
from memory_engine.ltm import ltm_store, maintenance_intent_compiler


def _id(char): return "mem_" + char * 32


def _object(object_id, title="Old"):
    return {"object_id": object_id, "object_type": "assertion", "title": title, "summary": None, "payload": {"schema_version": 1, "body": title}}


def _seed(root):
    event_stream.append_message({"role": "user", "content": "seed"}, root=root)
    plan = {"objects": [_object(_id("a"))], "links": [{"source": {"object_id": _id("a")}, "relation_type": "sourced_from", "target": {"event_slots": {"start": 0}}}]}
    assert ltm_store.commit_write_plan(plan, "seed", root)["status"] == "committed"


def test_compiler_expands_replace_and_resolves_temp_id(isolated_workspace):
    _seed(isolated_workspace)
    result = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [{"intent_type": "replace", "new_object": {"temp_id": "new", "object_type": "assertion", "title": "New", "payload": {"schema_version": 1, "body": "new"}}, "old_object_id": _id("a")} ]}, "run-1", 2, isolated_workspace)
    assert result["status"] == "compiled"
    new_id = result["temp_id_map"]["new"]
    assert result["idempotency_key"] == "maintenance:run-1:plan:2"
    assert result["links"] == [{"source": {"object_id": new_id}, "relation_type": "supersedes", "target": {"object_id": _id("a")}}]
    committed = ltm_store.commit_maintenance_plan(result, result["idempotency_key"], isolated_workspace)
    assert committed["status"] == "committed"
    assert ltm_store.get_object(new_id, root=isolated_workspace)["title"] == "New"


def test_compiler_rejects_conflicting_updates_and_allows_unanchored_object(isolated_workspace):
    _seed(isolated_workspace)
    conflict = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [{"intent_type": "update_title", "object_id": _id("a"), "new_title": "One"}, {"intent_type": "update_title", "object_id": _id("a"), "new_title": "Two"}]}, "run-2", 0, isolated_workspace)
    assert conflict["status"] == "rejected"
    compiled = maintenance_intent_compiler.compile_maintenance_action_plan({"atomic_operations": [{"operation_type": "create_object", "new_object": {"temp_id": "orphan", "object_type": "assertion", "title": "Orphan", "payload": {"schema_version": 1, "body": "orphan"}}}]}, "run-2", 1, isolated_workspace)
    assert ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], isolated_workspace)["status"] == "committed"
    missing = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [{"intent_type": "mark_support", "source_object_id": _id("a"), "target_object_id": _id("z")}]}, "run-2", 2, isolated_workspace)
    assert missing["status"] == "rejected"


def test_submit_maintenance_plan_schema_is_strict_and_atomic_create_only():
    schema = maintenance_intent_compiler.maintenance_action_plan_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"intents", "atomic_operations"}

    intent_variants = schema["properties"]["intents"]["items"]["oneOf"]
    intent_types = {variant["properties"]["intent_type"]["enum"][0] for variant in intent_variants}
    assert "create_object" not in intent_types
    assert "abstract" in intent_types
    assert all(variant["additionalProperties"] is False for variant in intent_variants)

    atomic_variants = schema["properties"]["atomic_operations"]["items"]["oneOf"]
    atomic_types = {variant["properties"]["operation_type"]["enum"][0] for variant in atomic_variants}
    assert "create_object" in atomic_types
    assert all(variant["additionalProperties"] is False for variant in atomic_variants)

    abstract_variants = [variant for variant in intent_variants if variant["properties"]["intent_type"]["enum"] == ["abstract"]]
    assert {variant["properties"]["relation_type"]["enum"][0] for variant in abstract_variants} == {"tagged_with", "supports"}
    assert {variant["properties"]["new_object"]["properties"]["object_type"]["enum"][0] for variant in abstract_variants} == {"tag", "assertion"}


def test_abstract_tagged_with_creates_direct_tag_links(isolated_workspace):
    _seed(isolated_workspace)
    plan = {
        "intents": [{
            "intent_type": "abstract",
            "relation_type": "tagged_with",
            "new_object": {
                "temp_id": "topic",
                "object_type": "tag",
                "title": "Sema project",
                "payload": {"schema_version": 1, "body": "Sema project topic"},
            },
            "source_object_ids": [_id("a")],
        }],
    }
    compiled = maintenance_intent_compiler.compile_maintenance_action_plan(plan, "run-abstract-tag", 0, isolated_workspace)
    assert compiled["status"] == "compiled", compiled
    tag_id = compiled["temp_id_map"]["topic"]
    assert compiled["links"] == [{"source": {"object_id": _id("a")}, "relation_type": "tagged_with", "target": {"object_id": tag_id}}]
    assert ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], isolated_workspace)["status"] == "committed"


def test_abstract_supports_creates_assertion_support_links(isolated_workspace):
    _seed(isolated_workspace)
    plan = {
        "intents": [{
            "intent_type": "abstract",
            "relation_type": "supports",
            "new_object": {
                "temp_id": "conclusion",
                "object_type": "assertion",
                "title": "Sema project conclusion",
                "payload": {"schema_version": 1, "body": "A higher-level conclusion."},
            },
            "source_object_ids": [_id("a")],
        }],
    }
    compiled = maintenance_intent_compiler.compile_maintenance_action_plan(plan, "run-abstract-support", 0, isolated_workspace)
    assert compiled["status"] == "compiled", compiled
    conclusion_id = compiled["temp_id_map"]["conclusion"]
    assert compiled["links"] == [{"source": {"object_id": _id("a")}, "relation_type": "supports", "target": {"object_id": conclusion_id}}]
    assert ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], isolated_workspace)["status"] == "committed"


def test_abstract_requires_explicit_relation_and_rejects_derived_from(isolated_workspace):
    _seed(isolated_workspace)
    base = {
        "intent_type": "abstract",
        "new_object": {"temp_id": "x", "object_type": "tag", "title": "Topic", "payload": {"schema_version": 1, "body": "topic"}},
        "source_object_ids": [_id("a")],
    }
    missing = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [base]}, "run-abstract-invalid", 0, isolated_workspace)
    assert missing["status"] == "rejected"
    assert "relation_type" in missing["errors"][0]["message"]
    wrong = dict(base, relation_type="derived_from")
    rejected = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [wrong]}, "run-abstract-invalid", 1, isolated_workspace)
    assert rejected["status"] == "rejected"
    assert "tagged_with or supports" in rejected["errors"][0]["message"]


def test_compiler_rejects_wrapper_and_aliases(isolated_workspace):
    _seed(isolated_workspace)
    wrapper = maintenance_intent_compiler.compile_maintenance_action_plan(
        {"maintenance_plan": {"atomic_operations": []}},
        "run-strict",
        0,
        isolated_workspace,
    )
    assert wrapper["status"] == "rejected"
    assert "unsupported top-level fields" in wrapper["errors"][0]["message"]

    wrong_intent = maintenance_intent_compiler.compile_maintenance_action_plan(
        {"intents": [{"intent_type": "create_object", "new_object": {"temp_id": "x"}}]},
        "run-strict",
        1,
        isolated_workspace,
    )
    assert wrong_intent["status"] == "rejected"
    assert "unsupported intent_type" in wrong_intent["errors"][0]["message"]

    action_alias = maintenance_intent_compiler.compile_maintenance_action_plan(
        {"atomic_operations": [{"action": "create_object", "new_object": {"temp_id": "x"}}]},
        "run-strict",
        2,
        isolated_workspace,
    )
    assert action_alias["status"] == "rejected"
    assert "unsupported atomic operation" in action_alias["errors"][0]["message"]


def test_maintenance_plan_is_atomic_and_plan_idempotent(isolated_workspace):
    _seed(isolated_workspace)
    link_id = ltm_store.get_links(_id("a"), "outgoing", root=isolated_workspace)[0]["link_id"]
    compiled = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [{"intent_type": "update_title", "object_id": _id("a"), "new_title": "Updated"}], "atomic_operations": [{"operation_type": "deactivate_link", "link_id": link_id}]}, "run-3", 0, isolated_workspace)
    first = ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], isolated_workspace)
    assert first["status"] == "committed", first
    assert ltm_store.get_object(_id("a"), root=isolated_workspace)["title"] == "Updated"
    assert ltm_store.get_links(_id("a"), "outgoing", root=isolated_workspace) == []
    assert ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], isolated_workspace)["status"] == "already_committed"


def test_maintenance_plan_vector_failure_leaves_no_partial_update(isolated_workspace, monkeypatch):
    _seed(isolated_workspace)
    compiled = maintenance_intent_compiler.compile_maintenance_action_plan({"intents": [{"intent_type": "update_title", "object_id": _id("a"), "new_title": "Never committed"}]}, "run-4", 0, isolated_workspace)
    monkeypatch.setattr(ltm_store, "_stage_vector_objects", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))
    result = ltm_store.commit_maintenance_plan(compiled, compiled["idempotency_key"], isolated_workspace)
    assert result["status"] == "rejected"
    assert ltm_store.get_object(_id("a"), root=isolated_workspace)["title"] == "Old"
