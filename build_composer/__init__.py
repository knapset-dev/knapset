"""
Pluggable Build & Set Composer Subsystem.

Houses modular combinatorial set synthesizers in `build_composer/modules/`.
"""

from build_composer.base import BaseBuildComposer, ComposerSetResult
from build_composer.registry import BuildComposerRegistry, get_composer_registry

__all__ = [
    "BaseBuildComposer",
    "ComposerSetResult",
    "BuildComposerRegistry",
    "get_composer_registry",
]
