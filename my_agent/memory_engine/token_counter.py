import json
from functools import lru_cache










TOKEN_ENCODING_NAME = "cl100k_base"



@lru_cache(maxsize=1)
def _encoding():
    try:
        import tiktoken
    except ImportError as error:
        raise RuntimeError("Missing tiktoken dependency. Install requirements.txt before running Sema.") from error
    return tiktoken.get_encoding(TOKEN_ENCODING_NAME)



def count_text_tokens(text):
    return len(_encoding().encode(str(text or "")))





def truncate_text_tokens(text, max_tokens, marker="\n[truncated by Runtime]\n"):
    normalized = str(text or "")
    limit = int(max_tokens)
    if limit <= 0:
        return ""
    tokens = _encoding().encode(normalized)
    if len(tokens) <= limit:
        return normalized
    marker_tokens = _encoding().encode(marker)
    if len(marker_tokens) >= limit:
        return _encoding().decode(tokens[:limit])
    available = limit - len(marker_tokens)
    head_count = (available + 1) // 2
    tail_count = available - head_count
    head = _encoding().decode(tokens[:head_count])
    tail = _encoding().decode(tokens[-tail_count:]) if tail_count else ""
    return head + marker + tail



def message_to_token_text(message):
    return json.dumps(dict(message or {}), ensure_ascii=False, sort_keys=True)



def count_message_tokens(message):
    return count_text_tokens(message_to_token_text(message))



def count_messages_tokens(messages):
    return sum(count_message_tokens(message) for message in messages or [])
