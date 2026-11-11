import sqlite3


def _message(role, content, **extra):
    return {"role": role, "content": content, **extra}


def test_pin_handoff_is_derived_from_same_stm_projection(isolated_workspace):
    import memory_engine.context_builder as context_builder
    import memory_engine.wm.pins as pins
    from memory_engine.stm_projection import build_projection_units, build_stm_projection
    from persistence import session

    messages = [
        _message("system", "system"),
        _message("user", "current task instruction"),
        _message("assistant", "latest work"),
    ]
    session.ensure_project()
    session.save_last_session(messages)
    budget = build_projection_units(messages)[-1]["token_count"]
    projection = build_stm_projection(messages, budget=budget)

    pin = pins.pin_record(pins.resolve_pin_for_projection(projection, messages, persist=True))
    wm = context_builder._build_working_memory_projection(projection, budget=1000, pin=pin)

    assert pin["source_slot_seq"] == 1
    assert "this might be important" in wm["content"]
    assert wm["content"].count("current task instruction") == 1
    assert 1 not in wm["selected_slot_seqs"]
    assert all(message.get("content") != "current task instruction" for message in projection["messages"])


def test_selected_user_slot_does_not_create_pin(isolated_workspace):
    import memory_engine.wm.pins as pins
    from memory_engine.stm_projection import build_stm_projection
    from persistence import session

    messages = [_message("system", "system"), _message("user", "current instruction")]
    session.ensure_project()
    session.save_last_session(messages)
    projection = build_stm_projection(messages, budget=1000)

    assert pins.resolve_pin_for_projection(projection, messages, persist=True) is None
    assert pins.load_current_pin(create=False) is None


def test_runtime_user_does_not_clear_pin_and_next_external_user_does(isolated_workspace):
    import memory_engine.wm.pins as pins
    from memory_engine.stm_projection import build_projection_units, build_stm_projection
    from persistence import session

    messages = [_message("system", "system"), _message("user", "current task"), _message("assistant", "work")]
    session.ensure_project()
    session.save_last_session(messages)
    budget = build_projection_units(messages)[-1]["token_count"]
    projection = build_stm_projection(messages, budget=budget)
    pins.resolve_pin_for_projection(projection, messages, persist=True)

    session.append_message_and_persist(messages, _message("user", "runtime wake", source="runtime"))
    assert pins.load_current_pin(create=False)["source_slot_seq"] == 1

    session.append_message_and_persist(messages, _message("user", "next external turn"))
    assert pins.load_current_pin(create=False) is None


def test_pin_renders_immutable_original_event(isolated_workspace):
    import memory_engine.event_stream as event_stream
    import memory_engine.wm.pins as pins
    from memory_engine.stm_projection import build_projection_units, build_stm_projection
    from persistence import session

    messages = [_message("system", "system"), _message("user", "exact original instruction"), _message("assistant", "work")]
    session.ensure_project()
    session.save_last_session(messages)
    event_stream.create_sum_event(1, {"summary": "compressed", "source_role": "user"})
    budget = build_projection_units(messages)[-1]["token_count"]
    projection = build_stm_projection(messages, budget=budget)

    pin = pins.pin_record(pins.resolve_pin_for_projection(projection, messages, persist=False))

    assert "exact original instruction" in pin["rendered"]
    assert "compressed" not in pin["rendered"]


def test_read_only_pin_derivation_does_not_create_table(isolated_workspace):
    import memory_engine.event_stream as event_stream
    import memory_engine.wm.pins as pins
    from memory_engine.stm_projection import build_projection_units, build_stm_projection
    from persistence import session

    messages = [_message("system", "system"), _message("user", "current task"), _message("assistant", "work")]
    session.ensure_project()
    session.save_last_session(messages)
    budget = build_projection_units(messages)[-1]["token_count"]
    projection = build_stm_projection(messages, budget=budget)

    assert pins.resolve_pin_for_projection(projection, messages, persist=False)["source_slot_seq"] == 1
    with sqlite3.connect(event_stream.event_stream_db_path()) as connection:
        table = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'working_memory_pins'"
        ).fetchone()
    assert table is None
