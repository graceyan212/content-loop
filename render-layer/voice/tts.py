"""tts.py — ElevenLabs /with-timestamps client, cached by content hash.

Caching is not an optimisation here. This API is billed per character, so a run
that dies between synthesis and render must not re-pay on retry.

That promise only holds if a *half-written* entry cannot be served. The presence
of the two files IS the cache predicate, so both halves are installed atomically
(tmp + fsync + os.replace) and re-validated on read. An entry that fails
validation — empty mp3, truncated/unparseable json, wrong shape — is reported as
a miss and overwritten by the miss path, so an interrupted write costs one
re-synthesis instead of poisoning that (voice, model, text) triple forever.

`alignment_source` is part of the return contract. The raw `alignment` track is
accepted only as a last resort and is always labelled, because a rate measured
from it is understated (the raw track holds "$5" for audio that speaks "five
dollars") and would silently mis-size every downstream script.
"""
from __future__ import annotations
import base64, binascii, hashlib, json, os, ssl, sys, urllib.error, urllib.request

API_ROOT = "https://api.elevenlabs.io"
KEY_ENV = "ELEVENLABS_API_KEY"


class TTSError(RuntimeError):
    pass


def _build_ssl_context() -> ssl.SSLContext:
    """A *verifying* TLS context with a CA bundle urllib can actually find.

    python.org's Python ships its own trust store and does not read the macOS
    keychain, so on a box where "Install Certificates.command" was never run that
    store is empty (measured: cert_store_stats()['x509_ca'] == 0). Every request
    then dies with CERTIFICATE_VERIFY_FAILED "unable to get local issuer
    certificate" — while curl, which does use the keychain, works fine, which is
    what makes this look like an ElevenLabs outage instead of a local trust gap.

    certifi carries the bundle, but it is imported defensively: a missing (or
    broken-install) certifi must not stop this module from importing, because
    cache_key/resolve_api_key and the whole cache-hit path need no network at all.

    The fallback is a plain default context, NOT an unverified one. On a box with
    an empty store that still fails — loudly, at the TLS handshake. That is the
    intended outcome: disabling verification would turn a connection error into a
    silently interceptable connection, which is strictly worse than the bug.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# Built once at import: parsing a ~140-cert PEM bundle per request is waste, and
# a single shared context lets TLS sessions be reused across the calls in a run.
_SSL_CONTEXT = _build_ssl_context()


def _ssl_context() -> ssl.SSLContext:
    """The context every outbound request uses. Exists as a seam so a test can
    assert the thing actually shipped verifies, rather than re-deriving its own."""
    return _SSL_CONTEXT


def resolve_api_key(search_roots) -> tuple[str, str]:
    """env -> .env.local -> .eleven.key, matching sffs-mobile-app's generate-audio.mjs."""
    v = (os.environ.get(KEY_ENV) or "").strip()
    if v:
        return v, f"env:{KEY_ENV}"
    for root in search_roots:
        envf = os.path.join(root, ".env.local")
        if os.path.isfile(envf):
            for line in open(envf, encoding="utf-8"):
                k, _, val = line.strip().partition("=")
                if k.strip() == KEY_ENV and val.strip():
                    return val.strip().strip("'\""), envf
        keyf = os.path.join(root, ".eleven.key")
        if os.path.isfile(keyf):
            val = open(keyf, encoding="utf-8").read().strip()
            if val:
                return val, keyf
    raise TTSError(
        f"no ElevenLabs key: set ${KEY_ENV}, or add .env.local / .eleven.key in "
        + ", ".join(search_roots))


def cache_key(voice_id: str, model_id: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(voice_id.encode()); h.update(b"\0")
    h.update(model_id.encode()); h.update(b"\0")
    h.update(text.encode())
    return h.hexdigest()


def _default_http(url: str, headers: dict, body: bytes) -> dict:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        # context= is load-bearing, not decoration: omit it and urllib falls back
        # to the interpreter's own (here: empty) trust store and no live call can
        # ever succeed. See _build_ssl_context.
        with urllib.request.urlopen(req, timeout=180, context=_ssl_context()) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise TTSError(f"ElevenLabs HTTP {e.code}: {e.read()[:300].decode('utf-8','ignore')}")
    except urllib.error.URLError as e:
        raise TTSError(f"ElevenLabs unreachable: {e}")


def _valid_alignment(a) -> bool:
    """Shape gate for a character-alignment track. Deliberately shallow: it only
    has to distinguish a usable track from junk (None, {}, a truncated dict, a
    ragged one). words.py owns the deep contract."""
    if not isinstance(a, dict):
        return False
    chars = a.get("characters")
    starts = a.get("character_start_times_seconds")
    ends = a.get("character_end_times_seconds")
    if not (isinstance(chars, list) and isinstance(starts, list)
            and isinstance(ends, list)):
        return False
    return len(chars) > 0 and len(chars) == len(starts) == len(ends)


def _write_bytes_atomic(path: str, data: bytes) -> None:
    tmp = path + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _write_json_atomic(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _read_cache_entry(mp3_path: str, align_path: str):
    """(alignment, alignment_source) for a *usable* entry, else None.

    Existence is not enough. A 0-byte mp3 or a truncated json would otherwise be
    served forever — and json.load would raise JSONDecodeError, which no caller
    catching TTSError can handle. Returning None routes the caller through the
    miss path, which overwrites the bad entry.
    """
    if not (os.path.isfile(mp3_path) and os.path.isfile(align_path)):
        return None
    try:
        if os.path.getsize(mp3_path) <= 0:
            return None
        with open(align_path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):  # ValueError covers json.JSONDecodeError
        return None
    if not isinstance(doc, dict):
        return None
    alignment = doc.get("alignment")
    source = doc.get("alignment_source")
    if source not in ("normalized", "raw") or not _valid_alignment(alignment):
        return None
    return alignment, source


def _pick_alignment(payload: dict) -> tuple[dict, str]:
    """normalized wins; raw is a labelled last resort, never a silent one."""
    normalized = payload.get("normalized_alignment")
    if _valid_alignment(normalized):
        return normalized, "normalized"
    raw = payload.get("alignment")
    if _valid_alignment(raw):
        sys.stderr.write(
            "WARNING: ElevenLabs returned no usable normalized_alignment; falling back "
            "to the raw `alignment` track (alignment_source='raw'). Captions will show "
            "unexpanded text ('$5', 'Dr.') and chars_per_second measured from this "
            "track is UNDERSTATED — do NOT record it as measured_cps.\n")
        return raw, "raw"
    raise TTSError("response carried no usable alignment: normalized_alignment and "
                   "alignment are both missing or malformed")


def synthesize(text, voice_id, model_id, cache_dir, api_key, http=None) -> dict:
    os.makedirs(cache_dir, exist_ok=True)
    key = cache_key(voice_id, model_id, text)
    mp3_path = os.path.join(cache_dir, f"{key}.mp3")
    align_path = os.path.join(cache_dir, f"{key}.align.json")

    hit = _read_cache_entry(mp3_path, align_path)
    if hit is not None:
        alignment, source = hit
        return {"mp3_path": mp3_path, "alignment": alignment,
                "alignment_source": source, "chars": len(text), "cached": True}

    url = f"{API_ROOT}/v1/text-to-speech/{voice_id}/with-timestamps"
    headers = {"xi-api-key": api_key, "content-type": "application/json",
               "accept": "application/json"}
    body = json.dumps({"text": text, "model_id": model_id}).encode()
    payload = (http or _default_http)(url, headers, body)
    if not isinstance(payload, dict):
        raise TTSError(f"ElevenLabs response was {type(payload).__name__}, "
                       "not a JSON object")

    audio_b64 = payload.get("audio_base64")
    if not audio_b64 or not isinstance(audio_b64, str):
        raise TTSError("response missing audio_base64")
    alignment, source = _pick_alignment(payload)

    # b64decode is lenient by default -- b64decode("!!!!") == b"" -- so a mangled
    # payload would install a 0-byte mp3 that only surfaces as an ffmpeg failure
    # two tasks downstream. Decode strictly, and prove there are bytes, before
    # anything reaches the cache.
    try:
        audio = base64.b64decode("".join(audio_b64.split()), validate=True)
    except (binascii.Error, ValueError) as e:
        raise TTSError(f"audio_base64 is not valid base64: {e}")
    if not audio:
        raise TTSError("audio_base64 decoded to 0 bytes of audio")

    # Both halves atomic, so the order below is immaterial to correctness: an
    # interrupt at any point leaves either no entry or a complete one.
    _write_json_atomic(align_path, {"alignment_source": source, "alignment": alignment})
    _write_bytes_atomic(mp3_path, audio)
    return {"mp3_path": mp3_path, "alignment": alignment,
            "alignment_source": source, "chars": len(text), "cached": False}
