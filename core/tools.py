import sys
import importlib
import pkgutil
import skills as _skills_pkg
import core.builtins as _builtins_pkg

SKILLS: dict = {}
TOOLS_CONFIG: list = []

# Builtins are loaded once at startup and are never hot-reloaded.
_BUILTIN_SKILLS: dict = {}
_BUILTIN_TOOLS_CONFIG: list = []

# Called after a new skill is created so the client re-registers with the server
_reregister_callback = None

def set_reregister_callback(fn):
    global _reregister_callback
    _reregister_callback = fn

def _load_builtins():
    """Load core/builtins/ skills once at startup. Not affected by reload_skills()."""
    for _finder, name, _ispkg in pkgutil.iter_modules(_builtins_pkg.__path__):
        full_name = f"core.builtins.{name}"
        try:
            module = importlib.import_module(full_name)
            # Single export
            if hasattr(module, "SKILL_FN") and hasattr(module, "SKILL_DEF"):
                fn_name = module.SKILL_DEF["function"]["name"]
                _BUILTIN_SKILLS[fn_name] = module.SKILL_FN
                _BUILTIN_TOOLS_CONFIG.append(module.SKILL_DEF)
            # Multiple exports from one file (e.g. task_context.py)
            if hasattr(module, "SKILL_FNS") and hasattr(module, "SKILL_DEFS"):
                for fn, defn in zip(module.SKILL_FNS, module.SKILL_DEFS):
                    fn_name = defn["function"]["name"]
                    _BUILTIN_SKILLS[fn_name] = fn
                    _BUILTIN_TOOLS_CONFIG.append(defn)
        except Exception as e:
            print(f"[builtins] Failed to load '{name}': {e}")

def reload_skills():
    """Rebuild SKILLS/TOOLS_CONFIG: builtins first, then hot-reload user skills/."""
    SKILLS.clear()
    TOOLS_CONFIG.clear()
    # Builtins are stable — copy them in first so user skills cannot override them
    SKILLS.update(_BUILTIN_SKILLS)
    TOOLS_CONFIG.extend(_BUILTIN_TOOLS_CONFIG)
    # Hot-reload user skills
    importlib.invalidate_caches()
    for _finder, name, _ispkg in pkgutil.iter_modules(_skills_pkg.__path__):
        full_name = f"skills.{name}"
        try:
            if full_name in sys.modules:
                module = importlib.reload(sys.modules[full_name])
            else:
                module = importlib.import_module(full_name)
            if hasattr(module, "SKILL_FN") and hasattr(module, "SKILL_DEF"):
                fn_name = module.SKILL_DEF["function"]["name"]
                SKILLS[fn_name] = module.SKILL_FN
                TOOLS_CONFIG.append(module.SKILL_DEF)
        except Exception as e:
            print(f"[client skills] Failed to load '{name}': {e}")
    if _reregister_callback:
        _reregister_callback()

_load_builtins()
reload_skills()

