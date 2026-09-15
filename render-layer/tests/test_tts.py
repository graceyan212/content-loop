import base64, json, os, ssl, sys, pytest
from voice import tts

ALIGN = {"characters": list("hi"),
         "character_start_times_seconds": [0.0, 0.1],
         "character_end_times_seconds": [0.1, 0.2]}

# Deliberately different numbers from ALIGN so a test can prove *which* track
# was used, not merely that some alignment came back.
RAW_ALIGN = {"characters": list("hi"),
             "character_start_times_seconds": [0.0, 0.5],
             "character_end_times_seconds": [0.5, 1.0]}

MP3_BYTES = b"ID3fake"

def _fake_http(calls):
    def http(url, headers, body):
        calls.append({"url": url, "headers": headers, "body": json.loads(body)})
        return {"audio_base64": base64.b64encode(MP3_BYTES).decode(),
                "normalized_alignment": ALIGN, "alignment": ALIGN}
    return http

def test_cache_key_changes_with_every_input():
    a = tts.cache_key("v1", "m1", "text")
    assert a != tts.cache_key("v2", "m1", "text")
    assert a != tts.cache_key("v1", "m2", "text")
    assert a != tts.cache_key("v1", "m1", "other")
    assert a == tts.cache_key("v1", "m1", "text")

def test_synthesize_writes_mp3_and_alignment(tmp_path):
    calls = []
    r = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert os.path.isfile(r["mp3_path"])
    # The payload, not just the path: without this the whole decode+write block
    # can be replaced by `open(mp3_path, "wb").close()` and the suite stays green.
    assert open(r["mp3_path"], "rb").read() == MP3_BYTES
    assert r["alignment"] == ALIGN
    assert r["cached"] is False
    assert r["chars"] == 2

def test_second_call_is_served_from_cache_without_http(tmp_path):
    calls = []
    tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    r2 = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert r2["cached"] is True
    assert len(calls) == 1  # the second call never hit http
    assert r2["alignment"] == ALIGN
    assert open(r2["mp3_path"], "rb").read() == MP3_BYTES

def test_uses_the_with_timestamps_endpoint_and_sends_the_key(tmp_path):
    calls = []
    tts.synthesize("hi", "voiceX", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert calls[0]["url"].endswith("/v1/text-to-speech/voiceX/with-timestamps")
    assert calls[0]["headers"]["xi-api-key"] == "KEY"
    # Authenticating correctly while asking ElevenLabs to synthesize nothing is a
    # live, billed 4xx. Assert the body we actually send.
    assert calls[0]["body"] == {"text": "hi", "model_id": "m1"}

def test_prefers_normalized_alignment(tmp_path):
    def http(url, headers, body):
        return {"audio_base64": base64.b64encode(b"x").decode(),
                "alignment": {"characters": ["W"],
                              "character_start_times_seconds": [0.0],
                              "character_end_times_seconds": [1.0]},
                "normalized_alignment": ALIGN}
    r = tts.synthesize("hi", "v", "m", str(tmp_path), "K", http=http)
    assert r["alignment"] == ALIGN
    assert r["alignment_source"] == "normalized"

def test_missing_key_raises(tmp_path, monkeypatch):
    # Without this delenv the test asserts a property of the *machine*, not of the
    # code: resolve_api_key consults $ELEVENLABS_API_KEY before search_roots, so
    # the test goes red on exactly the box where the pipeline runs for real.
    monkeypatch.delenv(tts.KEY_ENV, raising=False)
    with pytest.raises(tts.TTSError):
        tts.resolve_api_key([str(tmp_path)])

def test_api_key_never_lands_in_the_cache(tmp_path):
    tts.synthesize("hi", "v", "m", str(tmp_path), "SECRETKEY", http=_fake_http([]))
    for name in os.listdir(tmp_path):
        assert "SECRETKEY" not in open(os.path.join(tmp_path, name), "rb").read().decode("utf-8", "ignore")


# --- key resolution is hermetic in both directions ---------------------------

def test_env_key_wins_over_files(tmp_path, monkeypatch):
    (tmp_path / ".eleven.key").write_text("from-file")
    monkeypatch.setenv(tts.KEY_ENV, "from-env")
    assert tts.resolve_api_key([str(tmp_path)]) == ("from-env", f"env:{tts.KEY_ENV}")

def test_falls_back_to_key_file_when_env_is_unset(tmp_path, monkeypatch):
    monkeypatch.delenv(tts.KEY_ENV, raising=False)
    keyf = tmp_path / ".eleven.key"
    keyf.write_text("from-file\n")
    assert tts.resolve_api_key([str(tmp_path)]) == ("from-file", str(keyf))


# --- crash-safety of the cache entry ----------------------------------------

def test_both_cache_halves_are_installed_atomically(tmp_path, monkeypatch):
    """The entry's existence is the cache predicate, so a partially written half
    must never be visible. Installing through os.replace is the mechanism; this
    asserts it for BOTH files (previously only the mp3 had it, and nothing tested
    even that)."""
    real_replace = os.replace
    installed = []
    def spy(src, dst):
        installed.append((os.path.basename(str(src)), os.path.basename(str(dst))))
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", spy)
    tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http([]))
    key = tts.cache_key("v1", "m1", "hi")
    assert (f"{key}.align.json.tmp", f"{key}.align.json") in installed
    assert (f"{key}.mp3.tmp", f"{key}.mp3") in installed
    assert [n for n in os.listdir(tmp_path) if n.endswith(".tmp")] == []

@pytest.mark.parametrize("poison", [
    "",                                                  # interrupted at open("w")
    '{"alignment_source": "normalized", "alignment": {"characters": ["h", "i"], "chara',
    "not json at all",
    '{"characters": ["h", "i"]}',                         # legacy / wrong shape
])
def test_a_corrupt_alignment_half_is_a_miss_not_a_crash(tmp_path, poison):
    calls = []
    tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    key = tts.cache_key("v1", "m1", "hi")
    align_path = tmp_path / f"{key}.align.json"
    align_path.write_text(poison)

    r = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert r["cached"] is False          # re-synthesised rather than raising
    assert len(calls) == 2               # the poisoned entry did not serve
    assert r["alignment"] == ALIGN
    assert json.loads(align_path.read_text())["alignment"] == ALIGN  # self-healed

def test_a_zero_byte_mp3_half_is_a_miss_not_a_permanent_empty_render(tmp_path):
    calls = []
    r1 = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    open(r1["mp3_path"], "wb").close()
    assert os.path.getsize(r1["mp3_path"]) == 0

    r2 = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert r2["cached"] is False
    assert len(calls) == 2
    assert open(r2["mp3_path"], "rb").read() == MP3_BYTES


# --- payload validation ------------------------------------------------------

@pytest.mark.parametrize("audio_b64", ["!!!!", "", None, "===="])
def test_audio_that_cannot_decode_to_bytes_raises_ttserror(tmp_path, audio_b64):
    """base64.b64decode is lenient by default (b64decode("!!!!") == b""), so a
    mangled-but-JSON-parseable payload would otherwise install a 0-byte mp3."""
    def http(url, headers, body):
        return {"audio_base64": audio_b64, "normalized_alignment": ALIGN}
    with pytest.raises(tts.TTSError):
        tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=http)
    assert os.listdir(tmp_path) == []    # nothing was cached

@pytest.mark.parametrize("payload", [
    {"audio_base64": base64.b64encode(MP3_BYTES).decode()},
    {"audio_base64": base64.b64encode(MP3_BYTES).decode(),
     "normalized_alignment": None, "alignment": {}},
    {"audio_base64": base64.b64encode(MP3_BYTES).decode(),
     "normalized_alignment": {"characters": ["h", "i"],
                              "character_start_times_seconds": [0.0],
                              "character_end_times_seconds": [0.1, 0.2]}},  # ragged
])
def test_an_unusable_alignment_raises_ttserror(tmp_path, payload):
    with pytest.raises(tts.TTSError):
        tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY",
                       http=lambda u, h, b: payload)
    assert os.listdir(tmp_path) == []


# --- the raw-track fallback is recorded, never silent ------------------------

def test_raw_alignment_fallback_is_labelled_in_the_return_and_the_cache(tmp_path, capsys):
    """A run captioned/calibrated off the un-normalized track must be detectable:
    chars_per_second from the raw track is understated ("$5" is 2 characters of
    audio that speaks "five dollars"), and task 11 records that number as
    measured_cps."""
    def http(url, headers, body):
        return {"audio_base64": base64.b64encode(MP3_BYTES).decode(),
                "normalized_alignment": None, "alignment": RAW_ALIGN}
    r = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=http)
    assert r["alignment"] == RAW_ALIGN
    assert r["alignment_source"] == "raw"
    assert "normalized_alignment" in capsys.readouterr().err

    # and the label survives the cache round trip, so a later run cannot mistake
    # a raw-track entry for a normalized one
    r2 = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=http)
    assert r2["cached"] is True
    assert r2["alignment_source"] == "raw"
    assert r2["alignment"] == RAW_ALIGN

def test_normalized_source_survives_the_cache_round_trip(tmp_path):
    calls = []
    tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    r2 = tts.synthesize("hi", "v1", "m1", str(tmp_path), "KEY", http=_fake_http(calls))
    assert r2["cached"] is True
    assert r2["alignment_source"] == "normalized"


# --- the network path itself: TLS trust --------------------------------------
#
# Every test above injects a fake `http` into synthesize(), so _default_http --
# the only code in this module that ever opens a socket -- was executed by no
# test at all. The suite sat fully green while every live call on this machine
# died in the TLS handshake with CERTIFICATE_VERIFY_FAILED, because python.org's
# Python ignores the macOS keychain and its own trust store is empty here
# (measured: x509_ca == 0). A fix without these two tests just restores that
# hole. Neither one touches the network.

def test_ssl_context_verifies_and_has_ca_certificates_loaded():
    """Guards the half of the fix that is easy to get subtly wrong: a context can
    be *configured* to verify and still trust nothing. verify_mode and
    check_hostname both read correct against an empty store, and every handshake
    fails anyway with "unable to get local issuer certificate" -- which is exactly
    the bug. So the loaded-CA count is asserted too, not just the flags."""
    ctx = tts._ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
    assert ctx.cert_store_stats()["x509_ca"] > 0


def test_default_http_passes_a_verifying_ssl_context_to_urlopen(monkeypatch):
    """THE regression test for the reported defect, which was literally
    `urlopen(req, timeout=180)` with no context=. Drop the context argument from
    _default_http and this test goes red on `context is not None`.

    urlopen takes context as keyword-only, so capturing kwargs is sufficient --
    there is no positional form this could sneak through as."""
    seen = {}

    class _Resp:
        def __enter__(self): return self
        def __exit__(self, *exc): return False
        def read(self): return json.dumps({"audio_base64": "AA=="}).encode()

    def fake_urlopen(req, *args, **kwargs):
        seen["url"] = req.full_url
        seen["kwargs"] = kwargs
        return _Resp()

    monkeypatch.setattr(tts.urllib.request, "urlopen", fake_urlopen)
    out = tts._default_http("https://api.elevenlabs.io/v1/x", {"xi-api-key": "K"}, b"{}")

    assert out == {"audio_base64": "AA=="}          # the fake really was called
    assert seen["url"] == "https://api.elevenlabs.io/v1/x"
    ctx = seen["kwargs"].get("context")
    assert ctx is not None, "_default_http called urlopen with NO SSL context"
    assert isinstance(ctx, ssl.SSLContext)
    # and it is the verifying one, not a hand-rolled permissive context
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True


def test_context_still_verifies_when_certifi_is_unavailable(monkeypatch):
    """The certifi import is defensive so the module still imports without it --
    that must not become an escape hatch into an unverified context. Without a CA
    bundle the correct behaviour is a loud handshake failure, not a connection
    that silently skips verification."""
    monkeypatch.setitem(sys.modules, "certifi", None)   # makes `import certifi` raise
    ctx = tts._build_ssl_context()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    assert ctx.check_hostname is True
