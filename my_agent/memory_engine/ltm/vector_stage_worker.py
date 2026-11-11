"""Run native vector staging outside the backend process."""

import json
import sys

from .vector_index import VectorIndexAdapter


def main():
    try:
        payload = json.load(sys.stdin)
        staged = VectorIndexAdapter(payload["root"])._stage_upserts_in_process(
            payload["items"], remove_ids=payload["remove_ids"], token=payload["token"]
        )
        print(json.dumps({"status": "ok", "staged": staged}), flush=True)
    except Exception as error:
        print(json.dumps({"status": "error", "error": str(error)}), flush=True)


if __name__ == "__main__":
    main()
