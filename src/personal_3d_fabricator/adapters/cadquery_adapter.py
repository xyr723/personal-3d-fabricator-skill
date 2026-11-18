"""Load the optional CadQuery backend on demand."""

from dataclasses import dataclass, field
from types import ModuleType


@dataclass(frozen=True)
class CadQueryUnavailable:
    dependency: str
    reason: str
    available: bool = field(default=False, init=False)


def load_cadquery() -> ModuleType | CadQueryUnavailable:
    """Return the CadQuery module or a structured installation diagnostic."""
    try:
        import cadquery
    except ImportError as error:
        dependency = error.name or "cadquery"
        if isinstance(error, ModuleNotFoundError) and dependency == "cadquery":
            return CadQueryUnavailable(dependency, "cadquery extra not installed")
        return CadQueryUnavailable(dependency, f"CadQuery import failed: {error}")
    return cadquery
