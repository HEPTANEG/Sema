"""Local Qwen3 reranker service used by Memory Push Rank 2."""

import argparse
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


MODEL_NAME = "Qwen/Qwen3-Reranker-0.6B"

MAX_CONTEXT_TOKENS = 32768


MAX_BATCH_SIZE = 8
CPU_BATCH_SIZE = 1


class RerankerRequestError(ValueError):
    pass


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _validate_request(payload):
    if not isinstance(payload, dict):
        raise RerankerRequestError("request body must be a JSON object.")
    query = payload.get("query")
    candidates = payload.get("candidates")
    if not isinstance(query, str) or not query.strip():
        raise RerankerRequestError("query must be a non-empty string.")
    if not isinstance(candidates, list) or not candidates:
        raise RerankerRequestError("candidates must be a non-empty array.")
    for index, candidate in enumerate(candidates):
        if not isinstance(candidate, dict):
            raise RerankerRequestError(f"candidates[{index}] must be an object.")
        if not isinstance(candidate.get("object_id"), str) or not candidate["object_id"]:
            raise RerankerRequestError(f"candidates[{index}].object_id must be a string.")
        if not isinstance(candidate.get("text"), str):
            raise RerankerRequestError(f"candidates[{index}].text must be a string.")
    return query, candidates


def _load_model(model_path):
    path = Path(model_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(f"reranker model does not exist: {path}")
    try:
        import torch
        from sentence_transformers import CrossEncoder
    except ImportError as error:
        raise RuntimeError("Reranker Service requires sentence-transformers and torch.") from error
    device = "cuda" if torch.cuda.is_available() else "cpu"
    model = CrossEncoder(str(path), device=device, max_length=MAX_CONTEXT_TOKENS)
    model.model.eval()
    return model, device, torch


def _is_cuda_oom(error, torch):
    oom_error = getattr(getattr(torch, "cuda", None), "OutOfMemoryError", None)
    if oom_error is not None and isinstance(error, oom_error):
        return True
    return isinstance(error, RuntimeError) and "out of memory" in str(error).lower()


def _score(model, torch, device, query, candidates):
    pairs = [(query, candidate["text"]) for candidate in candidates]
    scores = []
    batch_size = min(MAX_BATCH_SIZE if device == "cuda" else CPU_BATCH_SIZE, len(pairs))
    start = 0
    while start < len(pairs):
        current_size = min(batch_size, len(pairs) - start)
        while True:
            batch = pairs[start : start + current_size]
            try:
                values = model.predict(
                    batch,
                    activation_fn=torch.nn.Sigmoid(),
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    batch_size=current_size,
                )
            except Exception as error:
                if device != "cuda" or current_size <= 1 or not _is_cuda_oom(error, torch):
                    raise
                torch.cuda.empty_cache()
                current_size //= 2
                continue
            scores.extend(float(value) for value in values)
            start += len(batch)
            batch_size = current_size
            break
    if len(scores) != len(candidates):
        raise RuntimeError("reranker returned an unexpected score count.")
    return scores


class _RerankerHandler(BaseHTTPRequestHandler):
    server_version = "SemaReranker/1"
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
                "device": self.server.device,
                "max_context_tokens": MAX_CONTEXT_TOKENS,
            },
        )

    def do_POST(self):
        if self.path != "/rerank":
            self._send_json(404, {"error": {"code": "not_found", "message": "endpoint not found."}})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 0:
                raise RerankerRequestError("Content-Length is required.")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            query, candidates = _validate_request(payload)
            with self.server.model_lock:
                scores = _score(self.server.model, self.server.torch, self.server.device, query, candidates)
            self._send_json(
                200,
                {
                    "model": self.server.model_name,
                    "scores": [
                        {"object_id": candidate["object_id"], "relevance_score": score}
                        for candidate, score in zip(candidates, scores)
                    ],
                },
            )
        except RerankerRequestError as error:
            self._send_json(400, {"error": {"code": "invalid_request", "message": str(error)}})
        except Exception as error:
            self._send_json(500, {"error": {"code": "rerank_failed", "message": str(error)}})

    def log_message(self, format, *args):
        print(format % args, file=sys.stderr, flush=True)


def _create_server(model, device, torch, port):
    server = ThreadingHTTPServer(("127.0.0.1", port), _RerankerHandler)
    server.model = model
    server.model_name = MODEL_NAME
    server.device = device
    server.torch = torch
    server.model_lock = threading.Lock()
    return server


def serve(model_path, port=0):
    model, device, torch = _load_model(model_path)
    server = _create_server(model, device, torch, port)
    print(json.dumps({"event": "ready", "port": server.server_address[1]}, separators=(",", ":")), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the local Sema Qwen3 reranker service.")
    parser.add_argument("--model-path", required=True)
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        serve(args.model_path, args.port)
    except Exception as error:
        print(str(error), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
