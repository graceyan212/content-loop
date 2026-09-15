# tests/test_footage_probe.py
import json, os, subprocess, pytest
from render import footage

def _make_clip(path, w, h, secs, with_audio):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-i",
           f"testsrc=size={w}x{h}:rate=30:duration={secs}"]
    if with_audio:
        cmd += ["-f", "lavfi", "-i",
                f"sine=frequency=440:duration={secs}", "-c:a", "aac"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(secs), path]
    subprocess.run(cmd, capture_output=True, check=True)

def _run(cmd):
    subprocess.run(cmd, capture_output=True, check=True)

def _make_clip_with_cover(path, w, h, secs, cover_w, cover_h, cover_first):
    """Mux a real footage stream together with a still-image cover-art
    stream disposed as attached_pic — the shape ffprobe reports for
    thumbnails embedded in .m4v/.mp4 files (e.g. an iTunes-style poster
    frame). probe_clip must pick the real footage stream regardless of
    which stream index the cover art lands on."""
    base = os.path.dirname(path)
    main = os.path.join(base, "_cover_main.mp4")
    cover_jpg = os.path.join(base, "_cover_still.jpg")
    cover_png = os.path.join(base, "_cover_still.png")
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
          f"testsrc=size={w}x{h}:rate=30:duration={secs}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", str(secs), main])
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"color=c=red:size={cover_w}x{cover_h}",
          "-frames:v", "1", cover_jpg])
    _run(["ffmpeg", "-y", "-i", cover_jpg, "-frames:v", "1", cover_png])
    if cover_first:
        inputs = ["-i", cover_png, "-i", main]
        disp = "-disposition:v:0"
        codecs = ["-c:v:0", "png", "-c:v:1", "copy"]
    else:
        inputs = ["-i", main, "-i", cover_png]
        disp = "-disposition:v:1"
        codecs = ["-c:v:0", "copy", "-c:v:1", "png"]
    _run(["ffmpeg", "-y", *inputs, "-map", "0", "-map", "1",
          *codecs, disp, "attached_pic", path])

def _make_multi_video_clip(path, specs):
    """Mux two or more genuine (non-attached-pic) video streams together —
    ambiguous footage, not a cover-art case."""
    inputs = []
    maps = []
    for i, (w, h, fps) in enumerate(specs):
        inputs += ["-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate={fps}:duration=1"]
        maps += ["-map", f"{i}:v"]
    _run(["ffmpeg", "-y", *inputs, *maps,
          "-c:v", "libx264", "-pix_fmt", "yuv420p", "-t", "1", path])

def test_probe_reports_measured_dimensions_and_audio(tmp_path):
    p = str(tmp_path / "a.mp4")
    _make_clip(p, 640, 360, 2, with_audio=True)
    info = footage.probe_clip(p)
    assert (info["width"], info["height"]) == (640, 360)
    assert info["has_audio"] is True
    assert 1.8 < info["duration_s"] < 2.3
    assert 29 < info["fps"] < 31

def test_probe_detects_missing_audio(tmp_path):
    p = str(tmp_path / "b.mp4")
    _make_clip(p, 360, 640, 1, with_audio=False)
    assert footage.probe_clip(p)["has_audio"] is False

def test_build_manifest_indexes_every_clip(tmp_path):
    for i, (w, h) in enumerate([(640, 360), (360, 640)]):
        _make_clip(str(tmp_path / f"c{i}.mp4"), w, h, 1, with_audio=False)
    m = footage.build_manifest(str(tmp_path))
    assert m["probed_count"] == 2
    assert len(m["clips"]) == 2

def test_probe_raises_on_nonvideo(tmp_path):
    p = tmp_path / "notvideo.mp4"
    p.write_text("nope")
    with pytest.raises(footage.ProbeError):
        footage.probe_clip(str(p))

def test_probe_ignores_attached_pic_cover_art(tmp_path):
    # Real-world shape: an .m4v with a real footage track plus a still-image
    # cover-art track muxed in and disposed as attached_pic (the mp4/mov
    # family of muxers always sorts attached_pic tracks after the regular
    # ones on write, so this only ever lands as a *trailing* stream here —
    # see test_probe_ignores_leading_attached_pic_cover_art below for the
    # index-0 ordering that the original bug report actually hit).
    p = str(tmp_path / "with_cover.m4v")
    _make_clip_with_cover(p, 640, 360, 1, cover_w=200, cover_h=200, cover_first=False)
    info = footage.probe_clip(p)
    assert (info["width"], info["height"]) == (640, 360)

def test_probe_ignores_leading_attached_pic_cover_art(monkeypatch, tmp_path):
    # Reproduces the exact shape from the bug report: ffprobe put the
    # attached-pic cover-art stream (200x200 mjpeg) at index 0, ahead of
    # the real footage (640x360/30fps h264), inside a muxed .mkv. No
    # locally-available muxer will reliably reorder tracks this way on
    # write (mp4/mov always push attached_pic to the end; mkv doesn't
    # round-trip the disposition flag on this ffmpeg build at all) — so
    # this stubs ffprobe's own JSON output to pin down the exact ordering
    # that broke stream selection, independent of container quirks.
    # probe_clip must not blindly take "the first video stream"; it must
    # skip attached_pic streams regardless of index.
    fake_probe_json = json.dumps({
        "streams": [
            {"index": 0, "codec_type": "video", "codec_name": "mjpeg",
             "width": 200, "height": 200, "r_frame_rate": "25/1",
             "disposition": {"attached_pic": 1}},
            {"index": 1, "codec_type": "video", "codec_name": "h264",
             "width": 640, "height": 360, "r_frame_rate": "30/1",
             "disposition": {"attached_pic": 0}},
        ],
        "format": {"duration": "2.000000"},
    })
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(args=cmd, returncode=0,
                                            stdout=fake_probe_json, stderr="")
    monkeypatch.setattr(footage.subprocess, "run", fake_run)
    p = str(tmp_path / "combined.mkv")
    open(p, "w").close()  # never actually read; ffprobe itself is stubbed
    info = footage.probe_clip(p)
    assert (info["width"], info["height"]) == (640, 360)
    assert info["fps"] == 30.0
    assert info["duration_s"] == 2.0

def test_probe_raises_on_ambiguous_multiple_video_streams(tmp_path):
    p = str(tmp_path / "multi.mkv")
    _make_multi_video_clip(p, [(640, 360, 30), (320, 240, 24)])
    with pytest.raises(footage.ProbeError):
        footage.probe_clip(p)

def test_build_manifest_persists_manifest_json_to_disk(tmp_path):
    for i, (w, h) in enumerate([(640, 360), (360, 640)]):
        _make_clip(str(tmp_path / f"c{i}.mp4"), w, h, 1, with_audio=False)
    m = footage.build_manifest(str(tmp_path))
    manifest_path = tmp_path / footage.MANIFEST_NAME
    assert manifest_path.is_file()
    on_disk = json.loads(manifest_path.read_text())
    assert on_disk == m
    assert on_disk["probed_count"] == 2

def test_build_manifest_raises_probe_error_on_missing_dir(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    with pytest.raises(footage.ProbeError):
        footage.build_manifest(missing)

def test_load_manifest_self_heals_when_manifest_missing(tmp_path):
    for i, (w, h) in enumerate([(640, 360), (360, 640)]):
        _make_clip(str(tmp_path / f"c{i}.mp4"), w, h, 1, with_audio=False)
    manifest_path = tmp_path / footage.MANIFEST_NAME
    assert not manifest_path.exists()
    m = footage.load_manifest(str(tmp_path))
    assert m["probed_count"] == 2
    assert len(m["clips"]) == 2
    assert manifest_path.is_file()  # self-healing must persist it, not just return it

def test_load_manifest_reads_from_disk_without_rebuilding(tmp_path):
    # A marker key build_manifest would never produce. If load_manifest
    # returns it unchanged, that proves the read-from-disk branch ran
    # rather than silently rebuilding over it.
    sentinel = {"clips": [], "probed_count": 999, "failed": [], "marker": "hand-crafted"}
    (tmp_path / footage.MANIFEST_NAME).write_text(json.dumps(sentinel))
    m = footage.load_manifest(str(tmp_path))
    assert m == sentinel

def test_load_manifest_raises_probe_error_on_missing_dir(tmp_path):
    missing = str(tmp_path / "does_not_exist")
    with pytest.raises(footage.ProbeError):
        footage.load_manifest(missing)

def _make_skewed_clip(path, secs_video, secs_audio):
    """Audio longer than picture — the ordinary shape of a screen recording, where
    the capture keeps the microphone open a moment after the last frame."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"testsrc=size=360x640:rate=30:duration={secs_video}",
         "-f", "lavfi", "-i", f"sine=frequency=440:duration={secs_audio}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", path],
        capture_output=True, check=True)
    return path

def test_probe_records_the_picture_duration_not_the_container_duration(tmp_path):
    # `duration_s` is what select_window allocates windows out of, so it has to be
    # how much PICTURE exists. format.duration is the container's, i.e. the longest
    # stream's: MEASURED on this fixture, video stream 3.000000s (nb_frames 90),
    # audio stream 3.200000s, format.duration 3.200000s. Recording 3.2 there made
    # every window over 2.97s one that compose.check_footage refuses, since it
    # measures the video stream (tolerance 1/30 + 1e-3 = 0.0343s).
    p = _make_skewed_clip(str(tmp_path / "skew.mp4"), 3.0, 3.2)
    raw = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
         "-show_format", p], capture_output=True, text=True, check=True).stdout)
    v = next(s for s in raw["streams"] if s["codec_type"] == "video")
    assert float(raw["format"]["duration"]) > float(v["duration"]), (
        "the fixture is not a/v skewed, so this test proves nothing")

    info = footage.probe_clip(p)
    assert info["duration_s"] == pytest.approx(float(v["duration"]), abs=1e-6)
    assert info["picture_duration_s"] == info["duration_s"]
    assert info["container_duration_s"] == pytest.approx(
        float(raw["format"]["duration"]), abs=1e-6)
    assert info["duration_s"] < info["container_duration_s"]

def test_probe_falls_back_to_the_container_duration_when_the_stream_has_none(
        monkeypatch, tmp_path):
    # matroska/webm typically carry no per-stream duration at all. Then the container
    # figure is the only measurement there is, and compose.check_footage resolves it
    # through the same function, so the two still agree.
    fake = json.dumps({
        "streams": [{"index": 0, "codec_type": "video", "codec_name": "vp9",
                     "width": 360, "height": 640, "r_frame_rate": "30/1"}],
        "format": {"duration": "4.500000"},
    })
    monkeypatch.setattr(footage.subprocess, "run",
                        lambda cmd, **kw: subprocess.CompletedProcess(
                            args=cmd, returncode=0, stdout=fake, stderr=""))
    p = str(tmp_path / "n.webm")
    open(p, "w").close()
    info = footage.probe_clip(p)
    assert info["duration_s"] == 4.5 and info["picture_duration_s"] == 4.5

def test_load_manifest_reprobes_clips_recorded_under_an_older_schema(tmp_path):
    # A manifest written before duration_s meant the picture duration holds numbers
    # that are wrong in the direction that aborts a render, and it reads perfectly
    # well, so nothing else would ever notice. Schema 1 shape, deliberately with a
    # duration that does not match the file on disk.
    _make_clip(str(tmp_path / "c0.mp4"), 360, 640, 1, with_audio=False)
    stale = {"clips": [{"path": str(tmp_path / "c0.mp4"), "duration_s": 99.0,
                        "width": 360, "height": 640, "fps": 30.0,
                        "has_audio": False}],
             "probed_count": 1, "failed": []}
    (tmp_path / footage.MANIFEST_NAME).write_text(json.dumps(stale))
    m = footage.load_manifest(str(tmp_path))
    assert m["schema"] == footage.PROBE_SCHEMA
    assert m["clips"][0]["duration_s"] < 2.0, (
        "the stale 99.0s duration was read back instead of re-measured")
    assert "picture_duration_s" in m["clips"][0]

@pytest.mark.parametrize("existing", ["stale-schema", "no-manifest"])
def test_load_manifest_measures_a_read_only_footage_dir_without_aborting(
        tmp_path, existing):
    # load_manifest is a READ of the clip library, and it must not turn a readable
    # footage directory into an abort. Once the stale-schema re-probe was added it
    # did exactly that: build_manifest raises ProbeError("could not write manifest
    # into ...") on a directory it cannot write, so a schema-1 manifest sitting in a
    # read-only library (a mounted archive, a shared folder owned by someone else)
    # made a load that previously SUCCEEDED start failing. The measurements are the
    # point; the file is a cache of them.
    footage_dir = tmp_path / "library"
    footage_dir.mkdir()
    _make_clip(str(footage_dir / "c0.mp4"), 360, 640, 1, with_audio=False)
    stale = footage_dir / footage.MANIFEST_NAME
    if existing == "stale-schema":
        stale.write_text(json.dumps(
            {"clips": [{"path": str(footage_dir / "c0.mp4"), "duration_s": 99.0,
                        "width": 360, "height": 640, "fps": 30.0,
                        "has_audio": False}],
             "probed_count": 1, "failed": []}))
        # Both halves of "read-only" are set, because they are not the same halt.
        # MEASURED with the manifest written straight through open(path, "w"): a
        # read-only DIRECTORY alone did NOT stop an existing manifest being rewritten
        # -- that call needs write permission on the FILE, not on its directory, so
        # with the dir at 0o500 and the manifest at 0o644 the rewrite succeeded and no
        # write error was recorded. The shape that failed was a read-only manifest
        # file: a read-only mount, or a library whose files belong to someone else.
        # `_persist` now writes a temp file and os.replace()s it, so an unwritable
        # directory stops it too.
        os.chmod(stale, 0o400)
    os.chmod(footage_dir, 0o500)
    try:
        # The precondition this test stands on: the directory really is unwritable.
        with pytest.raises(OSError):
            open(footage_dir / "canary.txt", "w").close()
        m = footage.load_manifest(str(footage_dir))
    finally:
        os.chmod(footage_dir, 0o700)
        if stale.exists():
            os.chmod(stale, 0o600)
    assert m["schema"] == footage.PROBE_SCHEMA
    assert m["probed_count"] == 1
    assert m["clips"][0]["duration_s"] < 2.0, (
        "the stale 99.0s duration was read back instead of re-measured")
    # Not silent: the caller can see why the next load will pay for the probes again.
    assert "could not write manifest" in m.get("manifest_write_error", ""), m

def test_build_manifest_still_raises_when_it_cannot_persist(tmp_path):
    # The other half of the above. A caller of build_manifest asked for the write, so
    # a directory it cannot write into is an error rather than something to work
    # around -- otherwise the read-only tolerance would have quietly removed the
    # guarantee that build_manifest leaves a manifest behind.
    footage_dir = tmp_path / "library"
    footage_dir.mkdir()
    _make_clip(str(footage_dir / "c0.mp4"), 360, 640, 1, with_audio=False)
    os.chmod(footage_dir, 0o500)
    try:
        with pytest.raises(footage.ProbeError) as e:
            footage.build_manifest(str(footage_dir))
    finally:
        os.chmod(footage_dir, 0o700)
    assert "could not write manifest" in str(e.value)

def test_a_failed_manifest_write_leaves_the_previous_manifest_intact(monkeypatch,
                                                                    tmp_path):
    # Once a failed write is something load_manifest carries on from, a HALF-written
    # manifest is the dangerous outcome: open(path, "w") truncates before it writes,
    # so a failure part-way through (full disk, mount vanishing) would leave a
    # truncated file that the next load refuses with "will not parse". The write goes
    # through a temp file and os.replace, so this simulates the failure at the write
    # itself and requires the old manifest to survive it byte-for-byte.
    _make_clip(str(tmp_path / "c0.mp4"), 360, 640, 1, with_audio=False)
    good = json.dumps({"schema": footage.PROBE_SCHEMA, "clips": [], "probed_count": 0,
                       "failed": [], "marker": "the manifest that must survive"})
    (tmp_path / footage.MANIFEST_NAME).write_text(good)

    def explode(*a, **kw):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(footage.json, "dump", explode)

    with pytest.raises(footage.ProbeError) as e:
        footage.build_manifest(str(tmp_path))
    assert "could not write manifest" in str(e.value)
    assert (tmp_path / footage.MANIFEST_NAME).read_text() == good, (
        "the previous manifest was truncated by a write that then failed")
    leftovers = [n for n in os.listdir(tmp_path) if ".tmp-" in n]
    assert leftovers == [], f"temp manifest files left behind: {leftovers}"

def test_probe_dir_writes_nothing(tmp_path):
    # The measuring half, separated from the persisting half so load_manifest has
    # something to call that cannot write.
    _make_clip(str(tmp_path / "c0.mp4"), 360, 640, 1, with_audio=False)
    before = sorted(os.listdir(tmp_path))
    m = footage.probe_dir(str(tmp_path))
    assert m["probed_count"] == 1 and m["schema"] == footage.PROBE_SCHEMA
    assert sorted(os.listdir(tmp_path)) == before, (
        "probe_dir touched the footage directory")

def test_load_manifest_does_not_reprobe_a_current_schema_manifest(tmp_path):
    # The other half: re-probing on every load would throw away the whole point of
    # the manifest. A marker key build_manifest would never produce, plus the current
    # schema number, must come back untouched.
    sentinel = {"schema": footage.PROBE_SCHEMA,
                "clips": [{"path": "/x/gone.mp4", "duration_s": 12.0, "width": 1080,
                           "height": 1920, "fps": 30.0, "has_audio": True,
                           "picture_duration_s": 12.0, "container_duration_s": 12.5}],
                "probed_count": 1, "failed": [], "marker": "hand-crafted"}
    (tmp_path / footage.MANIFEST_NAME).write_text(json.dumps(sentinel))
    assert footage.load_manifest(str(tmp_path)) == sentinel
