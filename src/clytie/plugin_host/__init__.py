"""插件宿主内部 API；插件只能依赖 clytie.sdk。"""

from .host import CORE_MODULES, HostPlan, PlanStale, PluginFailure, PluginHost
from .manifests import Manifest, ManifestError, discover, read_manifest

__all__ = [
    "CORE_MODULES",
    "HostPlan",
    "Manifest",
    "ManifestError",
    "PlanStale",
    "PluginFailure",
    "PluginHost",
    "discover",
    "read_manifest",
]
