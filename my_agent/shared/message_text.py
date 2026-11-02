def latest_user_text(messages):
    for message in reversed(messages or []):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(
                str(part.get("text") or "")
                for part in content
                if isinstance(part, dict)
            )
    return ""


def compact_content(content, limit=600):
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text") or ""))
            elif part.get("type") == "sema_image":
                parts.append(f"[image:{part.get('filename') or 'local image'}]")
        text = " ".join(parts)
    else:
        text = ""
    return compact_text(text, limit=limit)


def compact_text(text, limit=300):
    text = " ".join(str(text or "").split())
    if len(text) > limit:
        return text[:limit] + "..."
    return text
