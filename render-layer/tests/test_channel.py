import json, os, pytest, channel

def test_resolve_returns_absolute_root_and_config():
    ch = channel.resolve("main")
    assert ch["name"] == "main"
    assert os.path.isabs(ch["root"]) and os.path.isdir(ch["root"])
    assert ch["config"]["fps"] == 30

def test_unknown_channel_names_the_known_ones():
    with pytest.raises(channel.ChannelError) as e:
        channel.resolve("nope")
    assert "main" in str(e.value)

def test_watermark_defaults_to_empty_not_placeholder():
    assert channel.watermark(channel.resolve("main")) == ""
