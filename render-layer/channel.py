"""channel.py — channel resolution. The personas.py pattern, minus faces and briefs."""
from __future__ import annotations
import json, os

CODE_ROOT = os.path.dirname(os.path.abspath(__file__))
REGISTRY_PATH = os.path.join(CODE_ROOT, "channels.json")
DEFAULT_CHANNEL = "main"


class ChannelError(SystemExit):
    """Always carries an actionable message. Callers let it propagate."""


def _registry() -> dict:
    if not os.path.isfile(REGISTRY_PATH):
        raise ChannelError(f"FATAL: registry not found: {REGISTRY_PATH}")
    try:
        with open(REGISTRY_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ChannelError(f"FATAL: {REGISTRY_PATH} will not parse ({e})")


def resolve(name: str | None = None) -> dict:
    name = (name or DEFAULT_CHANNEL).strip().lower()
    reg = _registry()
    entry = reg.get(name)
    if entry is None:
        known = ", ".join(sorted(reg)) or "(none)"
        raise ChannelError(f"FATAL: unknown channel {name!r}. Known: {known}")
    root = os.path.normpath(os.path.join(CODE_ROOT, entry["root"]))
    if not os.path.isdir(root):
        raise ChannelError(f"FATAL: channel {name!r} root missing: {root}")
    cfg_path = os.path.join(root, "channel.json")
    if not os.path.isfile(cfg_path):
        raise ChannelError(f"FATAL: {cfg_path} missing")
    try:
        with open(cfg_path, encoding="utf-8") as fh:
            config = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        raise ChannelError(f"FATAL: {cfg_path} will not parse ({e})")
    return {"name": name, "root": root, "registry_entry": entry,
            "config": config, "channel_json_path": cfg_path}


def watermark(ch: dict) -> str:
    return (ch.get("config") or {}).get("watermark", "") or ""


def require_blog_id(ch: dict) -> str:
    bid = (ch.get("registry_entry") or {}).get("blog_id")
    if not bid:
        raise ChannelError(
            f"FATAL: channel {ch['name']!r} has no blog_id. Refusing to post.")
    return str(bid)
