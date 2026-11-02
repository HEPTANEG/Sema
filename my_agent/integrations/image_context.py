import base64
import shlex
from pathlib import Path

from model.provider import call_chat_completion, load_model_config, model_label, parse_chat_completion_message
from shared.prompt_loader import load_prompt
from tools import format_display_path, is_inside_workspace, resolve_user_path, resolve_workspace_directory













MAX_IMAGES_PER_MESSAGE = 10
SUPPORTED_IMAGE_SUFFIXES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}
MAX_VISION_ANALYSIS_IMAGES = 10


def parse_image_paths(text):

    return [item.strip().strip('"').strip("'") for item in shlex.split(text or "", posix=False) if item.strip()]


def validate_image_paths(paths, root=None, allow_external=True):
    if not paths:
        raise RuntimeError("Usage: /image <path...>")
    if len(paths) > MAX_IMAGES_PER_MESSAGE:
        raise RuntimeError(f"/image supports at most {MAX_IMAGES_PER_MESSAGE} images at once. Please split them into batches.")

    config = load_model_config()
    if not config.get("supports_vision"):
        raise RuntimeError(
            f"Current provider {model_label(config)} does not support image input. "
            "Use /config -> Use built-in profile -> kimi."
        )

    root = Path(root or Path.cwd())
    records = []
    for raw_path in paths:
        path = resolve_user_path(str(raw_path)) if root is None else (Path(raw_path).expanduser() if Path(raw_path).expanduser().is_absolute() else root / raw_path).resolve()
        suffix = path.suffix.lower()
        if suffix not in SUPPORTED_IMAGE_SUFFIXES:
            raise RuntimeError(f"Unsupported image type: {raw_path}. Supported: .png, .jpg, .jpeg, .webp")
        if not path.exists() or not path.is_file():
            raise RuntimeError(f"Image not found: {raw_path}")
        if not allow_external and not is_inside_workspace(path):
            raise RuntimeError("Cannot read image files outside the current working directory.")
        records.append(
            {
                "path": str(path),
                "filename": path.name,
                "mime_type": SUPPORTED_IMAGE_SUFFIXES[suffix],
            }
        )
    return records


def build_image_context_message(records):
    names = ", ".join(record["filename"] for record in records)
    content = [
        {
            "type": "text",
            "text": f"Image context added: {names}. Use these images in the next answer.",
        }
    ]
    for record in records:
        content.append(
            {
                "type": "sema_image",
                "path": record["path"],
                "filename": record["filename"],
                "mime_type": record["mime_type"],
            }
        )
    return {
        "role": "user",
        "content": content,
    }


def expire_image_context(messages):
    expired_count = 0
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue

        image_names = [
            part.get("filename") or Path(part.get("path", "")).name
            for part in content
            if isinstance(part, dict) and part.get("type") == "sema_image"
        ]
        if not image_names:
            continue

        expired_count += len(image_names)
        filtered = [
            part
            for part in content
            if not (isinstance(part, dict) and part.get("type") == "sema_image")
        ]
        marker = f"Expired image context, not sent again: {', '.join(image_names)}."


        if filtered and isinstance(filtered[0], dict) and filtered[0].get("type") == "text":
            filtered[0] = dict(filtered[0])
            filtered[0]["text"] = marker
        else:
            filtered.insert(0, {"type": "text", "text": marker})
        message["content"] = filtered
    return expired_count


def list_images_for_context(path_text, recursive=False, allow_external=False):
    path_text = (path_text or ".").strip() or "."
    directory_path = resolve_workspace_directory(path_text, allow_external=allow_external)
    iterator = directory_path.rglob("*") if recursive else directory_path.iterdir()
    image_paths = [
        path
        for path in iterator
        if path.is_file() and path.suffix.lower() in SUPPORTED_IMAGE_SUFFIXES
    ]
    image_paths = sorted(image_paths, key=lambda path: str(format_display_path(path)).lower())
    display = format_display_path(directory_path)

    lines = [
        f"--- images: {display} ---",
        f"count: {len(image_paths)}",
        f"recursive: {str(bool(recursive)).lower()}",
    ]
    if image_paths:
        for path in image_paths:
            lines.append(str(format_display_path(path)))
    else:
        lines.append("(no supported images)")
    lines.append("--- end ---")
    return "\n".join(lines)


def analyze_images_for_context(paths, instruction, allow_external=False):
    if not isinstance(paths, list) or not paths:
        raise RuntimeError("vision_analyze_images requires at least one image path.")
    if len(paths) > MAX_VISION_ANALYSIS_IMAGES:
        raise RuntimeError(
            f"vision_analyze_images supports at most {MAX_VISION_ANALYSIS_IMAGES} images at once. "
            "Split the task into smaller batches."
        )
    if not isinstance(instruction, str) or not instruction.strip():
        raise RuntimeError("vision_analyze_images requires a non-empty instruction.")

    records = validate_image_paths(paths, allow_external=allow_external)
    names = ", ".join(record["filename"] for record in records)
    message = build_image_context_message(records)
    message["content"][0]["text"] = (
        f"Analyze these image(s): {names}.\n\n"
        f"Instruction:\n{instruction.strip()}"
    )


    data = call_chat_completion(
        [
            {
                "role": "system",
                "content": load_prompt("vision_analysis"),
            },
            message,
        ],
        purpose="vision",
    )
    assistant_message = parse_chat_completion_message(data)
    content = assistant_message.get("content") or ""
    if not content.strip():
        raise RuntimeError("Vision provider returned an empty response.")

    image_lines = "\n".join(f"- {record['path']}" for record in records)
    return (
        "--- vision analysis ---\n"
        f"images:\n{image_lines}\n"
        f"instruction:\n{instruction.strip()}\n"
        "result:\n"
        f"{content.strip()}\n"
        "--- end ---"
    )


def image_part_to_data_url(part):
    path = Path(part.get("path", ""))
    if not path.exists() or not path.is_file():
        raise RuntimeError(f"Image file no longer exists: {path}")
    mime_type = part.get("mime_type") or SUPPORTED_IMAGE_SUFFIXES.get(path.suffix.lower())
    if not mime_type:
        raise RuntimeError(f"Unsupported image type: {path}")
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def has_image_parts(messages):
    for message in messages or []:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, dict) and part.get("type") in {"sema_image", "image_url"}:
                return True
    return False
