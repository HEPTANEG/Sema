from functools import lru_cache
from pathlib import Path


PROMPT_DIR = Path(__file__).resolve().parent.parent / "prompts"

PROMPT_FILES = {
    "main_agent": "main_agent.md",
    "working_memory": "working_memory.md",
    "focus_detector": "focus_detector.md",
    "segment_ingest": "segment_ingest.md",
    "full_segment_maintainer": "full_segment_maintainer.md",
    "ltm_maintainer": "ltm_maintainer.md",
    "formation": "formation.md",
    "memory_retrieval": "memory_retrieval.md",
    "memorize": "memorize.md",
    "memory_push_general_semantic": "memory_push_general_semantic.md",
    "memory_push_latest_semantic": "memory_push_latest_semantic.md",
    "memory_push_entity": "memory_push_entity.md",
    "memory_push_rank_3": "memory_push_rank_3.md",
    "vision_analysis": "vision_analysis.md",
}


MAIN_AGENT_PROMPT_FILES = (
    "main_agent",
)



@lru_cache(maxsize=None)
def load_prompt(name):
    filename = PROMPT_FILES.get(name)
    if not filename:
        raise RuntimeError(f"Unknown prompt: {name}")

    path = PROMPT_DIR / filename
    try:
        return path.read_text(encoding="utf-8").rstrip("\n")
    except FileNotFoundError as error:
        raise RuntimeError(f"Prompt file not found: {path}") from error


def compose_main_agent_prompt():
    return "\n\n".join(load_prompt(name) for name in MAIN_AGENT_PROMPT_FILES).rstrip("\n")
