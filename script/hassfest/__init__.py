"""Wrapper entry point for the hassfest validator."""

try:
    from hassfest.__main__ import main as _hassfest_main
except ModuleNotFoundError as exc:  # pragma: no cover - import error path
    raise ModuleNotFoundError(
        "hassfest is not installed. Install development dependencies with "
        "'python -m pip install -r requirements-dev.txt' before running this script."
    ) from exc


def main() -> None:
    """Execute the official hassfest entry point."""
    _hassfest_main()


__all__ = ["main"]
