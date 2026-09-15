#!/usr/bin/env python3
"""
test_persona_isolation.py — the invariant that kept getting broken.

Run:  python -m pytest test_persona_isolation.py -q

WHY THIS FILE EXISTS. Adding a third persona surfaced eight separate bugs of ONE
shape: code that resolves the persona correctly in one place and hardcodes `dani`
in another. Every one was silent — the pipeline ran green and produced wrong
output. The list, so nobody has to rediscover the pattern:

  1. loop/generate.py         personas.resolve(None) -> always Dani's brief, filed
                              into the requested persona's library
  2. loop/generate.py         SYSTEM prompt + hard-rules block hardcoded to Dani
  3. loop/generate.py         brief sliced at 2200 chars, silently truncating
                              Chloe's 3140 and dropping her hard limits
  4. batch.py                 DRAFT_FILES named Dani's two files literally
  5. batch.py                 TERRITORIES whitelisted Dani's four headings
  6. post/{schedule_batch,experiment,respin}.py
                              os.path.join(root, "character", "dani", ...) — a
                              persona-correct root with a Dani leaf. CRASH.
  7. loop/guard.py            one global kill switch and one global lock
  8. learn/dimensions.py      Dani's type universe + FALLBACK_DEFAULTS seeded into
                              any persona with no defaults.json
  9. loop/cycle.py            score.main([]) — empty argv, so --persona fell back
                              to DEFAULT_PERSONA and EVERY persona's cycle
                              REWROTE DANI'S learn/ data. The only one that
                              mutated another persona's state, and --dry-run did
                              not stop it.
 10. loop/cycle.py            pool counted only the gallery library, so a
                              drafts-*.md persona read as pool=0 and regenerated
                              every cycle forever

The through-line: a `root` that is correct plus a leaf that is a literal. These
tests encode the invariant rather than the ten instances, so the eleventh is caught
here instead of in production.
"""

from __future__ import annotations

import ast
import json
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
for p in (HERE, os.path.join(HERE, "copy"), os.path.join(HERE, "post"),
          os.path.join(HERE, "learn"), os.path.join(HERE, "loop")):
    if p not in sys.path:
        sys.path.insert(0, p)

import personas  # noqa: E402

PERSONA_NAMES = sorted(json.load(open(os.path.join(HERE, "personas.json"))))

# Every persona name that has ever been hardcoded somewhere, plus its data dir.
PERSONA_LITERALS = ("dani", "danielle", "chloe", "ray")


@pytest.fixture(params=PERSONA_NAMES)
def persona(request):
    return personas.resolve(request.param)


# ---------------------------------------------------------------------------
# 1. every persona-derived path lives under that persona's own root
# ---------------------------------------------------------------------------
def test_character_dir_is_under_own_root(persona):
    """Bug 6. character_dir must key on the persona NAME, not a literal."""
    d = personas.character_dir(persona)
    assert d.startswith(persona["root"] + os.sep), f"{persona['name']}: {d}"
    assert os.path.basename(d) == persona["name"]


def test_no_persona_path_contains_another_personas_name(persona):
    """The general form of bug 6: no path handed to a persona may contain any
    OTHER persona's name, in any segment."""
    import batch

    paths = dict(batch._persona_data_paths(persona))
    paths["character_dir"] = personas.character_dir(persona)

    others = [n for n in PERSONA_LITERALS
              if n != persona["name"] and not persona["root"].endswith(n)]
    for label, path in paths.items():
        if not isinstance(path, str):
            continue
        rel = path[len(persona["root"]):] if path.startswith(persona["root"]) else path
        segments = [s.lower() for s in rel.split(os.sep) if s]
        for other in others:
            assert other not in segments, (
                f"{persona['name']}: {label} = {path} contains foreign persona "
                f"segment {other!r}")


def test_loop_state_is_per_persona(persona):
    """Bug 7. Stopping or locking one persona must not stop or lock another."""
    import guard

    assert guard.lock_file(persona["name"], persona["root"]).startswith(
        persona["root"] + os.sep)
    assert persona["name"] in guard.stop_file(persona["name"])
    assert guard.kill_env(persona["name"]) == f"{persona['name'].upper()}_LOOP_KILL"


def test_lock_paths_are_distinct_across_personas():
    locks = {n: __import__("guard").lock_file(n, personas.resolve(n)["root"])
             for n in PERSONA_NAMES}
    assert len(set(locks.values())) == len(locks), f"shared lock file: {locks}"


# ---------------------------------------------------------------------------
# 2. no resolver helper may return another persona's value
# ---------------------------------------------------------------------------
def test_brief_is_this_personas_brief(persona):
    """Bug 1. The canary: a brief must name its own persona, never another."""
    if not (persona.get("config") or {}).get("brief"):
        pytest.skip(f"{persona['name']} has no brief configured yet")
    brief = personas.brief(persona).lower()
    display = (persona["config"].get("display_name") or persona["name"]).lower()
    assert display.split()[0] in brief, (
        f"{persona['name']}'s brief does not mention {display!r} — it is probably "
        f"another persona's brief")
    for other in PERSONA_NAMES:
        if other == persona["name"]:
            continue
        other_display = ((personas.resolve(other).get("config") or {})
                         .get("display_name") or other).lower().split()[0]
        if other_display == display.split()[0]:
            continue
        assert other_display not in brief, (
            f"{persona['name']}'s brief mentions {other_display!r}")


def test_generate_prompt_uses_the_requested_personas_brief(persona):
    """Bug 1, at the call site rather than the helper. build_prompt() must embed
    the brief of the persona it was HANDED, not personas.resolve(None)."""
    if not (persona.get("config") or {}).get("brief"):
        pytest.skip(f"{persona['name']} has no brief configured yet")
    import generate as gen

    prompt = gen.build_prompt(persona, 3)
    display = (persona["config"].get("display_name") or persona["name"]).split()[0]
    assert display.lower() in prompt.lower()
    assert prompt.count("You write as") == 1, "more than one persona brief in prompt"


def test_generate_prompt_is_not_truncated(persona):
    """Bug 3. The hard limits live at the END of every brief in this repo, so a
    silent truncation drops exactly the compliance text."""
    if not (persona.get("config") or {}).get("brief"):
        pytest.skip(f"{persona['name']} has no brief configured yet")
    import generate as gen

    brief = personas.brief(persona)
    assert len(brief) <= gen.BRIEF_MAX, (
        f"{persona['name']}'s brief is {len(brief)} chars, over BRIEF_MAX="
        f"{gen.BRIEF_MAX}; its tail would be silently cut from the prompt")
    assert brief in gen.build_prompt(persona, 3)


def test_hard_rules_are_not_inherited(persona):
    """Bug 2. A persona's rule block must come from its own persona.json."""
    import generate as gen

    block = gen._rules_block(persona)
    own = (persona.get("config") or {}).get("hard_rules") or []
    for rule in own:
        assert rule.strip() in block
    for other in PERSONA_NAMES:
        if other == persona["name"]:
            continue
        for rule in ((personas.resolve(other).get("config") or {})
                     .get("hard_rules") or []):
            if rule in own:
                continue
            assert rule.strip() not in block, (
                f"{persona['name']}'s rule block contains {other}'s rule: {rule!r}")


def test_copy_library_is_discovered_for_every_persona(persona):
    """Bugs 4 and 5. A persona with a library must not read as having none."""
    import batch

    copy_dir = batch._persona_data_paths(persona)["copy_dir"]
    files = batch.draft_files_in(copy_dir)
    on_disk = sorted(f for f in os.listdir(copy_dir)
                     if f.startswith("drafts-") and f.endswith(".md")) \
        if os.path.isdir(copy_dir) else []
    assert files == on_disk, "draft_files_in() disagrees with what is on disk"
    if not on_disk:
        pytest.skip(f"{persona['name']} has no drafts-*.md library")
    hooks = batch.parse_hooks(copy_dir=copy_dir, files=files,
                              territories=batch.persona_territories(persona))
    assert hooks, (
        f"{persona['name']} has {len(on_disk)} draft file(s) but parsed ZERO hooks "
        f"— its `## <territory>` headings are probably not in its territories list")


def test_dimensions_universe_is_this_personas(persona):
    """Bug 8. Ray must not be A/B tested on Dani's content types."""
    import dimensions as D

    universe = D.dimensions_for(persona)
    declared = (persona.get("config") or {}).get("territories")
    if declared:
        assert universe["type"] == tuple(declared)
    slots = [s["name"] for s in ((persona.get("config") or {})
                                 .get("posting_slots") or []) if s.get("name")]
    if slots:
        assert universe["slot"] == tuple(slots)


def test_no_fabricated_defaults_for_a_persona_with_none(persona):
    """Bug 8, the dangerous half: seeding Dani's reigning choices into a persona
    that has never posted invents a decision nobody made, and build_arms() would
    then test four content types that persona has no copy for."""
    import dimensions as D

    seed = D.fallback_defaults_for(persona)
    universe = D.dimensions_for(persona)
    for dim, val in seed.items():
        assert val in universe.get(dim, ()), (
            f"{persona['name']} seeded {dim}={val!r}, which is not in its own "
            f"universe {universe.get(dim)}")
    if not seed:
        assert D.build_arms(seed, universe) == [], (
            "no defaults must mean no arms, not fabricated ones")


# ---------------------------------------------------------------------------
# 3. static scan — the exact bug shape, not an approximation of it
# ---------------------------------------------------------------------------
def _py_files():
    for dirpath, dirnames, filenames in os.walk(HERE):
        dirnames[:] = [d for d in dirnames
                       if d not in ("__pycache__", ".pytest_cache", "node_modules")]
        for f in filenames:
            if f.endswith(".py"):
                yield os.path.join(dirpath, f)


# AST, NOT REGEX. The first cut of these two scans matched source text and both
# fired on DOCSTRINGS — the comments in personas.py and cycle.py that quote the old
# buggy pattern in order to explain it. A textual scan cannot tell code from prose
# about code, and a check that flags its own documentation is worse than no check.
# ast.parse sees only syntax, so a literal inside a docstring is a Constant nobody
# is calling.


def _trees():
    for path in _py_files():
        if os.path.basename(path) == os.path.basename(__file__):
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                yield path, ast.parse(fh.read())
        except (SyntaxError, UnicodeDecodeError):
            continue


def _is_os_path_join(call: ast.Call) -> bool:
    f = call.func
    return (isinstance(f, ast.Attribute) and f.attr == "join"
            and isinstance(f.value, ast.Attribute) and f.value.attr == "path")


def _is_persona_root(node: ast.AST) -> bool:
    """`root`, `p["root"]`, `persona["root"]` — a path that is already correct."""
    if isinstance(node, ast.Name) and node.id == "root":
        return True
    if isinstance(node, ast.Subscript):
        sl = node.slice
        return isinstance(sl, ast.Constant) and sl.value == "root"
    return False


def _bad_joins(tree: ast.AST) -> list[int]:
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _is_os_path_join(node) and node.args):
            continue
        if not _is_persona_root(node.args[0]):
            continue
        for a in node.args[1:]:
            if (isinstance(a, ast.Constant) and isinstance(a.value, str)
                    and a.value.lower() in PERSONA_LITERALS):
                out.append(node.lineno)
    return out


def _mains_without_persona(tree: ast.AST) -> list[int]:
    out = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "main" and len(node.args) == 1):
            continue
        argv = node.args[0]
        if not isinstance(argv, ast.List):
            continue
        strs = [e.value for e in argv.elts
                if isinstance(e, ast.Constant) and isinstance(e.value, str)]
        if "--persona" not in strs:
            out.append(node.lineno)
    return out


def test_the_detectors_actually_detect():
    """A green scan is only reassuring if the scan can go red. Both detectors
    silently fired on prose once; prove they fire on real code and only on it."""
    bad_join = ast.parse('import os\nx = os.path.join(root, "character", "dani", "m.json")\n')
    assert _bad_joins(bad_join), "bad-join detector missed the literal bug-6 pattern"

    good_join = ast.parse('import os\nx = os.path.join(root, "character", name, "m.json")\n')
    assert not _bad_joins(good_join), "bad-join detector flags the correct form"

    prose = ast.parse('def f():\n    """os.path.join(root, "character", "dani") is wrong."""\n')
    assert not _bad_joins(prose), "bad-join detector still fires on a docstring"

    bad_main = ast.parse("import score\nscore.main([])\n")
    assert _mains_without_persona(bad_main), "argv detector missed score.main([])"

    good_main = ast.parse('import score\nscore.main(["--persona", n])\n')
    assert not _mains_without_persona(good_main), "argv detector flags the correct form"

    prose_main = ast.parse('def f():\n    """do not call score.main([]) — it defaults."""\n')
    assert not _mains_without_persona(prose_main), "argv detector still fires on a docstring"


# Entrypoints whose --persona may stay defaulted: the output is a throwaway local
# preview you look at immediately, so picking the wrong persona costs you one glance
# and nothing else. EVERYTHING ELSE must require the flag.
DEFAULT_ALLOWED = {
    "gallery.py",        # writes an html preview
    "learn/plan.py",     # read-only report
    "post/review.py",    # writes html to /tmp
    # asset generators, explicitly scoped to one persona by design
    "render/build_docs.py",
    "render/gen_assets.py",
}


def _persona_arg_is_required(tree: ast.AST):
    """(found, required) for this module's --persona declaration."""
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        if not (node.args and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "--persona"):
            continue
        for kw in node.keywords:
            if kw.arg == "required" and isinstance(kw.value, ast.Constant):
                return True, bool(kw.value.value)
        return True, False
    return False, False


def test_state_changing_entrypoints_require_persona():
    """`DEFAULT_PERSONA = "dani"` is one global answer to "who did you mean when you
    didn't say" — it cannot be made per-persona, so with three personas the only
    safe move is to stop answering. Note `default=None` counts as defaulted: it
    flows into resolve(None) -> DEFAULT_PERSONA, which is "dani" with extra steps
    and does not even look like a default at the call site.
    """
    offenders = []
    for path, tree in _trees():
        rel = os.path.relpath(path, HERE)
        if rel in DEFAULT_ALLOWED or rel.startswith("test_"):
            continue
        found, required = _persona_arg_is_required(tree)
        if found and not required:
            offenders.append(rel)
    assert not offenders, (
        "these entrypoints can post to a brand or write persona state, and their "
        "--persona is optional — so running them without the flag silently operates "
        "on DEFAULT_PERSONA ('dani'). Add required=True, or add the file to "
        f"DEFAULT_ALLOWED if its output is genuinely a throwaway preview.\n  "
        + "\n  ".join(sorted(offenders)))


def test_no_literal_persona_name_joined_onto_a_persona_root():
    offenders = [f"{os.path.relpath(p, HERE)}:{ln}"
                 for p, t in _trees() for ln in _bad_joins(t)]
    assert not offenders, (
        "a literal persona name is joined onto a persona-derived root. That is the "
        "crash in bug 6 — every other persona resolves to a directory that cannot "
        "exist, and schedule_batch.py is the loop's make phase. Use "
        "personas.character_dir(persona).\n  " + "\n  ".join(offenders))


def test_no_module_main_is_called_without_a_persona():
    offenders = [f"{os.path.relpath(p, HERE)}:{ln}"
                 for p, t in _trees() for ln in _mains_without_persona(t)]
    assert not offenders, (
        "a module's main() is invoked with an argv that omits --persona, so it falls "
        "back to personas.DEFAULT_PERSONA ('dani') and operates on the WRONG "
        "persona's data. This is bug 9: a DRY RUN of Ray's cycle rewrote Dani's "
        "learn/ files.\n  " + "\n  ".join(offenders))
