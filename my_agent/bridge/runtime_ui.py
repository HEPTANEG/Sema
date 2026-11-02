from contextlib import nullcontext


def model_status():
    return nullcontext()


def tool_status(_tool, _args):
    return nullcontext()


def render_markdown(_text):
    return None


def render_tool_progress(_tool, _args, _text, verbose=False):
    return None
