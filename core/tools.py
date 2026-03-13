import sys
import importlib
import pkgutil
import skills as _skills_pkg

SKILLS: dict = {}
TOOLS_CONFIG: list = []

# Called after a new skill is created so the client re-registers with the server
_reregister_callback = None

def set_reregister_callback(fn):
    global _reregister_callback
    _reregister_callback = fn

def reload_skills():
    SKILLS.clear()
    TOOLS_CONFIG.clear()
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

reload_skills()
