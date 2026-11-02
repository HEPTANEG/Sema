"""Small validation helpers for runtime dependency seams."""


class RuntimeDependencyError(RuntimeError):
    """Raised when a runtime boundary receives an incomplete dependency source."""


def validate_dependencies(dependencies, required, owner):
    """Validate required dependency keys and report all missing keys at once."""
    source = dependencies or {}
    missing = [key for key in required if key not in source]
    if missing:
        missing_text = ", ".join(dict.fromkeys(missing))
        raise RuntimeDependencyError(f"{owner} missing runtime dependencies: {missing_text}")
    return source
