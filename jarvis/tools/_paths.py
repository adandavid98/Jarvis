"""Path resolution helpers shared across Jarvis tools."""

from __future__ import annotations

from pathlib import Path


def resolve_workspace_path(
    workspace_dir: Path,
    raw_path: str,
    *,
    allow_outside: bool = False,
) -> Path:
    """Resolve a user/model-supplied path against the configured workspace root.

    Raises:
        ValueError: If `allow_outside` is False and the resolved path escapes `workspace_dir`.
    """
    base = workspace_dir.resolve()
    candidate = Path(raw_path).expanduser()
    resolved = candidate.resolve() if candidate.is_absolute() else (base / candidate).resolve()

    if not allow_outside:
        try:
            resolved.relative_to(base)
        except ValueError as exc:
            raise ValueError(
                f"Path '{raw_path}' resolves outside workspace '{base}'."
            ) from exc

    return resolved
