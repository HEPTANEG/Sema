import pytest


def test_object_reference_map_registers_and_resolves_stably():
    from memory_engine.ltm.object_references import ObjectReferenceMap

    refs = ObjectReferenceMap()
    assert refs.register("mem_a") == 1
    assert refs.register("mem_a") == 1
    assert refs.register_many(["mem_b", "mem_c"]) == {2: "mem_b", 3: "mem_c"}
    assert refs.ref_for("mem_c") == 3
    assert refs.resolve(2) == "mem_b"
    assert refs.resolve_many([1, 3]) == ["mem_a", "mem_c"]


@pytest.mark.parametrize("value", [True, False, 0, -1, 1.0, "1", None])
def test_object_reference_map_rejects_invalid_references(value):
    from memory_engine.ltm.object_references import ObjectReferenceError, ObjectReferenceMap

    refs = ObjectReferenceMap()
    refs.register("mem_a")
    with pytest.raises(ObjectReferenceError):
        refs.resolve(value)


def test_object_reference_map_rejects_unknown_and_duplicate_refs():
    from memory_engine.ltm.object_references import ObjectReferenceError, ObjectReferenceMap

    refs = ObjectReferenceMap()
    refs.register("mem_a")
    with pytest.raises(ObjectReferenceError):
        refs.resolve(2)
    with pytest.raises(ObjectReferenceError):
        refs.resolve_many([1, 1])


def test_object_reference_maps_are_scope_local():
    from memory_engine.ltm.object_references import ObjectReferenceMap

    first = ObjectReferenceMap()
    second = ObjectReferenceMap()
    assert first.register("mem_a") == 1
    assert second.register("mem_other") == 1
    assert first.resolve(1) == "mem_a"
    assert second.resolve(1) == "mem_other"
