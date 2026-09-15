import os, pytest, capability

FONTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "assets", "fonts")

def test_required_filters_present_on_this_machine():
    f = capability.ffmpeg_filters()
    for name in ("overlay", "gblur", "scale", "crop", "concat",
                 "amix", "loudnorm", "anullsrc", "trim", "atrim"):
        assert name in f, f"missing filter {name}"

def test_text_filters_are_known_absent():
    # Documents the constraint the whole caption design rests on.
    f = capability.ffmpeg_filters()
    assert not ({"ass", "subtitles", "drawtext"} & f)

def test_check_passes_with_real_fonts():
    capability.check(FONTS)

def test_check_names_the_missing_font(tmp_path):
    with pytest.raises(capability.CapabilityError) as e:
        capability.check(str(tmp_path))
    assert "TikTokSans-Variable.ttf" in str(e.value)
