"""Local BGE-M3 embedding service used by the Memory Engine adapter."""

import argparse
import json
import math
import sys
import threading
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODEL_NAME = "BAAI/bge-m3"
EXPECTED_DIMENSION = 1024
ALLOWED_INPUT_KINDS = frozenset({"document", "query"})


class EmbeddingRequestError(ValueError):
    """Raised when an /embed request is not valid."""


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _validate_request(payload):
    if not isinstance(payload, dict):
        raise EmbeddingRequestError("request body must be a JSON object.")
    texts = payload.get("texts")
    if not isinstance(texts, list) or not texts:
        raise EmbeddingRequestError("texts must be a non-empty array.")
    if any(not isinstance(text, str) for text in texts):
        raise EmbeddingRequestError("every item in texts must be a string.")
    input_kind = payload.get("input_kind")
    if input_kind not in ALLOWED_INPUT_KINDS:
        raise EmbeddingRequestError("input_kind must be document or query.")
    return texts, input_kind


def _validate_vectors(vectors, expected_count):
    rows = vectors.tolist() if hasattr(vectors, "tolist") else vectors
    if not isinstance(rows, list) or len(rows) != expected_count:
        raise RuntimeError("embedding service returned an unexpected vector count.")
    normalized = []
    for row in rows:
        if not isinstance(row, list) or len(row) != EXPECTED_DIMENSION:
            raise RuntimeError("BGE-M3 returned an unexpected vector dimension.")
        values = [float(value) for value in row]
        if any(not math.isfinite(value) for value in values):
            raise RuntimeError("embedding service returned a non-finite vector.")
        norm = math.sqrt(sum(value * value for value in values))
        if not math.isclose(norm, 1.0, rel_tol=1e-3, abs_tol=1e-3):
            raise RuntimeError("embedding service returned a non-normalized vector.")
        normalized.append(values)
    return normalized


def _load_model(model_path):
    path = Path(model_path).expanduser()
    if not path.is_dir():
        raise FileNotFoundError(f"embedding model directory does not exist: {path}")
    try:
        import torch
        from sentence_transformers import SentenceTransformer
    except ImportError as error:
        raise RuntimeError(
            "Embedding Service requires sentence-transformers and torch."
        ) from error

    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = SentenceTransformer(str(path), device=device)
    dimension = int(model.get_sentence_embedding_dimension())
    if dimension != EXPECTED_DIMENSION:
        raise RuntimeError(
            f"BGE-M3 embedding dimension must be {EXPECTED_DIMENSION}, got {dimension}."
        )
    return model, device


class _EmbeddingHandler(BaseHTTPRequestHandler):
    server_version = "SemaEmbedding/1"
    protocol_version = "HTTP/1.0"

    def _send_json(self, status, payload):
        body = _json_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path != "/health":
            self._send_json(404, {"error": {"code": "not_found", "message": "endpoint not found."}})
            return
        self._send_json(
            200,
            {
                "status": "ready",
                "model": self.server.model_name,
                "dimension": self.server.dimension,
                "device": self.server.device,
            },
        )

    def do_POST(self):
        if self.path != "/embed":
            self._send_json(404, {"error": {"code": "not_found", "message": "endpoint not found."}})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0:
                raise EmbeddingRequestError("Content-Length is required.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            texts, _input_kind = _validate_request(payload)
            with self.server.model_lock:
                vectors = self.server.model.encode(
                    texts,
                    normalize_embeddings=True,
                    convert_to_numpy=True,
                    show_progress_bar=False,
                )
            normalized = _validate_vectors(vectors, len(texts))
            self._send_json(
                200,
                {
                    "model": self.server.model_name,
                    "dimension": self.server.dimension,
                    "vectors": normalized,
                },
            )
        except EmbeddingRequestError as error:
            self._send_json(400, {"error": {"code": "invalid_request", "message": str(error)}})
        except Exception as error:
            self._send_json(500, {"error": {"code": "embedding_failed", "message": str(error)}})

    def log_message(self, format, *args):
        print(format % args, file=sys.stderr, flush=True)


def _create_server(model, device, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), _EmbeddingHandler)
    server.model = model
    server.model_name = MODEL_NAME
    server.dimension = EXPECTED_DIMENSION
    server.device = device
    server.model_lock = threading.Lock()
    return server


def _shutdown_when_stream_closes(server, stream):
    try:
        stream.read()
    finally:
        server.shutdown()


def serve(model_path, port=0, shutdown_on_stdin_eof=False):

    with redirect_stdout(sys.stderr):
        model, device = _load_model(model_path)
    server = _create_server(model, device, port)
    if shutdown_on_stdin_eof:
        threading.Thread(
            target=_shutdown_when_stream_closes,
            args=(server, sys.stdin),
            daemon=True,
        ).start()
    print(
        json.dumps(
            {"event": "ready", "port": server.server_address[1]},
            separators=(",", ":"),
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local Sema BGE-M3 embedding service.")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--shutdown-on-stdin-eof", action="store_true")
    args = parser.parse_args(argv)
    try:
        serve(args.model_path, args.port, shutdown_on_stdin_eof=args.shutdown_on_stdin_eof)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
