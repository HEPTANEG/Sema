import io
import json
import threading
import time

from bridge.backend_server import BackendServer
from bridge.backend_transport import pipe_name
from bridge.ui_bridge_server import JsonRpcError


class FakeClient:
    def __init__(self):
        self.messages = []

    def write(self, payload):
        self.messages.append(payload)

    def close(self):
        return None


class RequestStreamClient(FakeClient):
    def __init__(self, payloads):
        super().__init__()
        self.payloads = payloads

    def read_chunks(self):
        yield ("\n".join(json.dumps(payload) for payload in self.payloads) + "\n").encode()


def test_backend_dispatches_confirmation_while_turn_request_is_waiting():
    turn_started = threading.Event()
    confirmation_received = threading.Event()

    class BlockingBackend(BackendServer):
        def dispatch(self, method, params):
            if method == "message.send":
                turn_started.set()
                assert confirmation_received.wait(timeout=1)
                return {"ok": True}
            if method == "confirmation.respond":
                confirmation_received.set()
                return {"ok": True}
            raise AssertionError(method)

    server = BlockingBackend(input_stream=io.StringIO(), output_stream=io.StringIO())
    client = RequestStreamClient(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "message.send", "params": {"prompt": "edit"}},
            {"jsonrpc": "2.0", "id": 2, "method": "confirmation.respond", "params": {"choice": "yes"}},
        ]
    )

    server._serve_client(client)
    deadline = time.time() + 1
    while len(client.messages) < 2 and time.time() < deadline:
        time.sleep(0.01)

    assert turn_started.is_set()
    assert confirmation_received.is_set()
    assert {message["id"] for message in client.messages} == {1, 2}


def test_backend_pipe_name_is_stable_per_workspace(tmp_path):
    first = pipe_name(tmp_path)
    second = pipe_name(tmp_path)
    other = pipe_name(tmp_path / "other")

    assert first == second
    assert first.startswith(r"\\.\pipe\sema-backend-")
    assert first != other


def test_backend_initialize_is_idempotent(isolated_workspace):
    server = BackendServer(input_stream=io.StringIO(), output_stream=io.StringIO())

    first = server.initialize({})
    first_project_id = first["project"]["project_id"]
    second = server.initialize({})

    assert second["project"]["project_id"] == first_project_id
    assert server.backend_initialized is True
    assert len(server.messages) == 1


def test_backend_broadcasts_one_event_to_all_clients():
    server = BackendServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    first = FakeClient()
    second = FakeClient()
    server.clients.update({first, second})

    server.send_event("model_thinking", {"path": "structured"}, turn_id="turn-1")

    assert len(first.messages) == 1
    assert len(second.messages) == 1
    assert first.messages[0] == second.messages[0]
    assert first.messages[0]["params"]["turn_id"] == "turn-1"


def test_backend_broadcast_event_accepts_and_preserves_wake_correlation_ids():
    server = BackendServer(input_stream=io.StringIO(), output_stream=io.StringIO())
    client = FakeClient()
    server.clients.add(client)

    server.send_event(
        "wake_delivered",
        {"wake_id": "wake-1", "source": "maintainer", "kind": "maintenance_task_resumed"},
        turn_id="turn-1",
        request_id="request-1",
        parent_id="turn-parent-1",
    )

    assert client.messages[0]["params"] == {
        "event_id": client.messages[0]["params"]["event_id"],
        "turn_id": "turn-1",
        "request_id": "request-1",
        "parent_id": "turn-parent-1",
        "type": "wake_delivered",
        "sequence": 1,
        "payload": {"wake_id": "wake-1", "source": "maintainer", "kind": "maintenance_task_resumed"},
    }


def test_backend_preserves_json_rpc_error_codes():
    class BusyBackend(BackendServer):
        def dispatch(self, method, params):
            raise JsonRpcError(-32010, "A turn is already running.")

    server = BusyBackend(input_stream=io.StringIO(), output_stream=io.StringIO())
    client = FakeClient()

    server._handle_client_payload(
        client,
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "message.send", "params": {}}).encode(),
    )

    assert client.messages == [{
        "jsonrpc": "2.0",
        "id": 7,
        "error": {"code": -32010, "message": "A turn is already running.", "data": None},
    }]
