"""Every `store.X` / `ai_store.X` the app calls must exist in app.ai.store.

No database needed. A bad merge (Oct 7) dropped three store functions the weekend columns call; the DB tests that
would have caught it were skipped, so production returned 500 on /api/weekend and the Tuesday column failed 6 times.
"""
import ast
from pathlib import Path

from app.ai import store

APP = Path(__file__).resolve().parents[1] / "app"
ALIASES = {"store", "ai_store"}


def _called_store_names():
    found = {}
    for path in APP.rglob("*.py"):
        if path.name == "store.py" and path.parent.name == "ai":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name)
                    and node.value.id in ALIASES):
                found.setdefault(node.attr, path.relative_to(APP).as_posix())
    return found


def test_every_store_function_the_app_calls_exists():
    missing = {name: where for name, where in _called_store_names().items() if not hasattr(store, name)}
    assert not missing, f"called but not defined in app/ai/store.py: {missing}"


def test_the_scan_sees_the_weekend_functions():
    # guards the guard: the scan must actually find the calls it is meant to check
    assert {"latest_weekend", "save_weekend", "weekend_rejected_out"} <= set(_called_store_names())
