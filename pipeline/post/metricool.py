#!/usr/bin/env python3
"""
metricool.py — minimal Metricool API client, shared across every persona.

WHY THIS SHAPE: the published docs page is an overview; the real contract is
`/api/swagger.json` (504 paths). Rather than hardcode field names from notes,
`schema` pulls the live ScheduledPost definition so a mismatch shows up as a
readable diff instead of a silent 400. Metricool also fails *quietly* on at
least one field — `providers` must be a list of OBJECTS (`[{"network":"tiktok"}]`);
passing bare strings returns 200 and simply never schedules anything.

Auth is a custom header, NOT Bearer:
    X-Mc-Auth: <token>
and userId + blogId ride in the QUERY STRING on every call.

Credentials (token + userId) are read from ~/.dani-metricool.env (chmod 600),
never from argv, never printed, and NEVER per-persona / never in the repo —
that file is the one place they live, for every persona.

blogId is different: it identifies WHICH brand a post goes to, so it must
come from the persona being acted on, not just whatever the env file
happens to have. Pass --persona to resolve it from personas.json /
<persona>/persona.json; a persona with no blogId configured (e.g. a
scaffolded persona whose Metricool brand does not exist yet) makes every
command that would touch the API FAIL LOUDLY instead of silently falling
back to the env file's blogId (= someone else's brand). Omit --persona and
behavior is unchanged: the env file's METRICOOL_BLOG_ID, exactly as before.

Commands:
    python metricool.py brands                     # find your blogId
    python metricool.py schema                     # dump ScheduledPost fields
    python metricool.py normalize --url URL        # URL -> mediaId
    python metricool.py post --config post.json [--publish]
    python metricool.py --persona dani post --config post.json --publish

`post` defaults to creating a DRAFT. `--publish` is what makes it real.

DRAFT -> SCHEDULED, the hard way. There is no in-place flag flip:
  * PATCH /v2/scheduler/posts/{id} accepts ONLY `publicationDate` (reschedule); it
    rejects `draft` and `autoPublish` outright, and 500s without a `fields` query
    param naming what you are changing.
  * PUT /v2/scheduler/posts/{id} with a create-shaped body DOES work, but it is a
    REPLACE: the old id 404s afterwards and a NEW id appears. Echoing the GET
    response back as the PUT body 500s, because it carries read-only fields.
So: build a clean create-shaped body, PUT it, then LIST the scheduler to recover the
new id. Never assume the id you created with is still valid.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CODE_ROOT = os.path.dirname(HERE)
if CODE_ROOT not in sys.path:
    sys.path.insert(0, CODE_ROOT)
import personas  # noqa: E402  (personas.py — persona registry + resolution)

BASE = "https://app.metricool.com/api"
ENV_PATH = os.path.expanduser("~/.dani-metricool.env")
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36"


def _ssl_context() -> ssl.SSLContext:
    """
    This machine's python.org build ships no CA bundle, so the stock context
    fails with CERTIFICATE_VERIFY_FAILED against any https host. certifi is
    installed, so prefer it and keep verification ON. Only fall back to an
    unverified context if certifi is genuinely absent, and say so loudly - we
    are sending an auth token over this connection.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        print("WARNING: certifi unavailable; TLS verification DISABLED. "
              "Install certifi (`pip install certifi`) before sending real "
              "credentials over this connection.", file=sys.stderr)
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx


_SSL = _ssl_context()


def load_creds(blog_id: str | None = None) -> dict:
    """Read token/userId/blogId from the env file, falling back to os.environ.

    `blog_id`, if given, OVERRIDES whatever the env file has for
    METRICOOL_BLOG_ID — this is how a resolved persona's blogId takes
    precedence over the env file. Callers that pass nothing (every call site
    that predates personas.py) get exactly the old behavior: the env file's
    METRICOOL_BLOG_ID, or none at all if that isn't set either.
    """
    creds = {}
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                # Strip an inline `# comment` — a blogId written as
                # "1000001  # persona-a-tiktok" silently produced a malformed query
                # param and returned ANOTHER brand's posts, which is a very
                # confusing failure. Never trust the tail of the line.
                v = v.split("#", 1)[0]
                creds[k.strip()] = v.strip().strip('"').strip("'")
    for k in ("METRICOOL_TOKEN", "METRICOOL_USER_ID", "METRICOOL_BLOG_ID"):
        if k not in creds and os.environ.get(k):
            creds[k] = os.environ[k]
    missing = [k for k in ("METRICOOL_TOKEN", "METRICOOL_USER_ID") if not creds.get(k)]
    if missing:
        raise SystemExit(
            f"missing {', '.join(missing)}.\n"
            f"Create {ENV_PATH} with:\n"
            f"  METRICOOL_TOKEN=...\n  METRICOOL_USER_ID=...\n  METRICOOL_BLOG_ID=...\n"
            f"then: chmod 600 {ENV_PATH}"
        )
    if blog_id:
        creds["METRICOOL_BLOG_ID"] = blog_id
    return creds


def _req(method: str, path: str, creds: dict, params: dict | None = None,
         body: dict | None = None, base: str = BASE):
    params = dict(params or {})
    params.setdefault("userId", creds["METRICOOL_USER_ID"])
    if creds.get("METRICOOL_BLOG_ID") and "blogId" not in params:
        params["blogId"] = creds["METRICOOL_BLOG_ID"]
    url = f"{base}{path}"
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})

    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Mc-Auth", creds["METRICOOL_TOKEN"])
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, context=_SSL, timeout=60) as r:
            raw = r.read().decode("utf-8", "replace")
            return r.status, (json.loads(raw) if raw.strip() else None)
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = {"raw": raw[:2000]}
        return e.code, parsed


# ---------------------------------------------------------------------------
# introspection
# ---------------------------------------------------------------------------
def fetch_schema(name: str = "ScheduledPost") -> dict:
    """Pull a definition out of the live swagger so field names aren't guesses."""
    req = urllib.request.Request(f"{BASE}/swagger.json")
    req.add_header("User-Agent", UA)
    with urllib.request.urlopen(req, context=_SSL, timeout=120) as r:
        spec = json.loads(r.read().decode("utf-8", "replace"))
    defs = spec.get("components", {}).get("schemas", spec.get("definitions", {}))
    out = {}
    for key in defs:
        if key.lower() == name.lower() or key.lower().startswith(name.lower()):
            out[key] = defs[key]
    return out


# ---------------------------------------------------------------------------
# operations
# ---------------------------------------------------------------------------
def list_brands(creds: dict):
    for path in ("/v2/settings/brands", "/settings/brands", "/admin/simpleProfiles"):
        status, body = _req("GET", path, creds, params={"blogId": None})
        if status == 200 and body:
            return path, body
    return None, None


def normalize_media(creds: dict, url: str):
    return _req("GET", "/actions/normalize/image/url", creds, params={"url": url})


def create_post(creds: dict, spec: dict, publish: bool = False):
    """
    spec keys: text, media_url|media_id, networks[], scheduled_at, timezone,
               first_comment, is_aigc, music_id
    Draft unless publish=True.
    """
    networks = spec.get("networks") or ["tiktok"]
    body: dict = {
        # objects, not strings - the string form returns 200 and schedules nothing
        "providers": [{"network": n} for n in networks],
        "text": spec.get("text", ""),
        "autoPublish": bool(publish),
        "draft": not publish,
    }
    if spec.get("scheduled_at"):
        body["publicationDate"] = {
            "dateTime": spec["scheduled_at"],
            "timezone": spec.get("timezone", "America/Chicago"),
        }
    if spec.get("first_comment"):
        body["firstCommentText"] = spec["first_comment"]
    media = spec.get("media_id") or spec.get("media_url")
    if media:
        # Verified against the live schema: the field is `media` (array of
        # strings). There is no `mediaUrls`.
        body["media"] = [media] if isinstance(media, str) else list(media)
        # Make Metricool COPY the file instead of hotlinking it. Without this a
        # presigned S3 URL has to still be valid at publish time; with it the
        # signature only needs to survive the submit call. Essential for
        # anything scheduled more than a few hours out.
        body["saveExternalMediaFiles"] = True
    if spec.get("alt_text"):
        body["mediaAltText"] = [spec["alt_text"]]

    # PER-NETWORK PUBLISH CONFIG. Omitting any of this returns 200 at schedule
    # time and then FAILS AT PUBLISH, hours or days later, which is the worst
    # possible place to find out. Two real failures, both from posts recreated
    # through this function rather than through schedule_batch:
    #
    #   tiktok    "Publish Tiktok photo error: does not specified privacy options"
    #             -> privacyOption is REQUIRED for a photo post. It was never set
    #                here; schedule_batch sets it inline, so only this path broke.
    #   instagram "The VIDEO value for media_type is deprecated. Use the REELS
    #             media type" -> a video needs type=REEL on both facebookData and
    #             instagramData, or Instagram rejects it outright.
    #
    # Both took out a live product post. Defaults match what schedule_batch sends.
    if "tiktok" in networks:
        tiktok: dict = {
            "privacyOption": spec.get("privacy_option", "PUBLIC_TO_EVERYONE"),
            "photoCoverIndex": 0,
            "disableComment": False,
            "autoAddMusic": spec.get("auto_add_music", True),
        }
        if spec.get("is_aigc") is not None:
            tiktok["isAigc"] = bool(spec["is_aigc"])
        if spec.get("music_id"):
            tiktok["music"] = {"musicId": spec["music_id"]}
        body["tiktokData"] = tiktok

    # A video on Facebook/Instagram must be declared a REEL.
    _m = media if isinstance(media, str) else (list(media)[0] if media else "")
    is_video = str(_m).lower().split("?")[0].endswith((".mp4", ".mov", ".m4v"))
    if "facebook" in networks and is_video:
        body["facebookData"] = {"type": "REEL"}
    if "instagram" in networks:
        ig: dict = {"autoPublish": True, "isAiGenerated": False}
        if is_video:
            ig.update({"type": "REEL", "showReelOnFeed": True})
        body["instagramData"] = ig

    return _req("POST", "/v2/scheduler/posts", creds, body=body), body


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--persona", required=True,
                    help="persona name from personas.json. REQUIRED: this command "
                         "talks to a live Metricool brand. It resolves that persona's "
                         "blogId (failing loudly if it has none) instead of falling "
                         "back to whatever ~/.dani-metricool.env holds — which, with "
                         "three personas, is how you post as the wrong person.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("brands")
    s = sub.add_parser("schema"); s.add_argument("--name", default="ScheduledPost")
    n = sub.add_parser("normalize"); n.add_argument("--url", required=True)
    p = sub.add_parser("post")
    p.add_argument("--config", required=True, help="JSON file with the post spec")
    p.add_argument("--publish", action="store_true", help="actually publish (default: draft)")
    a = ap.parse_args(argv)

    if a.cmd == "schema":
        print(json.dumps(fetch_schema(a.name), indent=2)[:12000])
        return 0

    persona_blog_id = None
    if a.persona:
        # require_blog_id() raises PersonaError (a clear FATAL message) rather
        # than returning None — a persona with no blogId configured must never
        # silently fall back to the env file's (someone else's) brand.
        persona_blog_id = personas.require_blog_id(personas.resolve(a.persona))

    creds = load_creds(blog_id=persona_blog_id)

    if a.cmd == "brands":
        path, body = list_brands(creds)
        print(f"endpoint: {path}")
        rows = (body or {}).get("data") if isinstance(body, dict) else body
        rows = rows if isinstance(rows, list) else []

        # One line per brand, and NEVER truncated. This used to print
        # json.dumps(body)[:6000], which at ~700 chars per brand silently dropped
        # everything past the seventh. The whole purpose of this command is to find
        # a blogId, and the brands it hid were the most recently created ones —
        # which are exactly the ones you are looking for when you run it. A tool
        # that silently returns a partial answer is worse than one that errors.
        for r in rows:
            if not isinstance(r, dict):
                continue
            nets = r.get("networksData") or {}
            conn = ", ".join(sorted(k.replace("Data", "") for k in nets)) or "no networks connected"
            print(f"  {r.get('id')}  {str(r.get('label') or '?'):<32} {conn}")
        print(f"\n{len(rows)} brand(s). Put the id in that persona's "
              f"persona.json as metricool.blog_id.")
        return 0 if rows else 1

    if a.cmd == "normalize":
        status, body = normalize_media(creds, a.url)
        print(status, json.dumps(body, indent=2)[:3000])
        return 0 if status == 200 else 1

    if a.cmd == "post":
        with open(a.config) as fh:
            spec = json.load(fh)
        (status, body), sent = create_post(creds, spec, publish=a.publish)
        print("--- request body ---")
        print(json.dumps(sent, indent=2))
        print(f"--- response {status} ---")
        print(json.dumps(body, indent=2)[:4000])
        if status not in (200, 201):
            return 1
        # Metricool can 200 while scheduling nothing; surface the per-network status.
        for d in (body or {}).get("descendants", []) or []:
            print("network status:", d.get("providers") or d.get("status"))
        return 0

    return 1


if __name__ == "__main__":
    sys.exit(main())
