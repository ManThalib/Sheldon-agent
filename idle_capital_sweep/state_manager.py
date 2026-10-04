import json
import os


def state_path(state_dir: str = None) -> str:
    if state_dir is None:
        state_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state")
    return os.path.join(state_dir, "add_state.json")


def load_add_state(state_dir: str = None) -> dict:
    """Load sweep state. Missing or malformed fails safe to empty state."""
    try:
        with open(state_path(state_dir), "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def save_add_state(state: dict, state_dir: str = None) -> bool:
    """Persist sweep state atomically (tmp + rename)."""
    try:
        path = state_path(state_dir)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, path)
        return True
    except OSError:
        return False