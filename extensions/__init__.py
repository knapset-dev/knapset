"""Domain Extensions Package."""
from pkgutil import extend_path

__path__ = extend_path(__path__, __name__)

try:
    from app_paths import get_subsystem_dirs

    for _ext_dir in get_subsystem_dirs("extensions"):
        _ext_str = str(_ext_dir)
        if _ext_str not in __path__ and _ext_dir.is_dir():
            __path__.append(_ext_str)
except Exception:
    pass

