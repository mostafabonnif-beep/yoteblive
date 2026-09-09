"""اختبارات آلية لوحدات youtube-live-relay الأساسية."""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402


def make_config(**overrides):
    data = {
        "source_url": "https://example.com/live",
        "rtmp_base": "rtmp://example.com/live",
        "stream_keys": ["key1"],
    }
    data.update(overrides)
    return app.RelayConfig.from_dict(data)


class TestConfigParsing:
    def test_resolution_sanitized(self):
        assert make_config(resolution="BAD").resolution == "1280x720"
        assert make_config(resolution=" 1920X1080 ").resolution == "1920x1080"

    def test_fps_clamped(self):
        assert make_config(fps="999").fps == 120
        assert make_config(fps="-5").fps == 1
        assert make_config(fps="abc").fps == 30

    def test_delays_clamped_and_ordered(self):
        cfg = make_config(reconnect_delay="1", max_reconnect_delay="99999")
        assert cfg.reconnect_delay == 3
        assert cfg.max_reconnect_delay == 900
        cfg = make_config(reconnect_delay=100, max_reconnect_delay=10)
        assert cfg.max_reconnect_delay >= cfg.reconnect_delay

    def test_health_timeout_bounds(self):
        assert make_config(health_timeout="1").health_timeout == 15
        assert make_config(health_timeout="99999").health_timeout == 600

    def test_stall_timeout_bounds(self):
        assert make_config(stall_timeout="5").stall_timeout == 10
        assert make_config(stall_timeout="99999").stall_timeout == 300
        assert make_config(stall_timeout="abc").stall_timeout == 15
        assert make_config().stall_timeout == 15

    def test_max_session_minutes_bounds(self):
        assert make_config(max_session_minutes="-5").max_session_minutes == 0
        assert make_config(max_session_minutes="99999").max_session_minutes == 720
        assert make_config(max_session_minutes="abc").max_session_minutes == 120
        assert make_config().max_session_minutes == 120

    def test_preset_validated(self):
        assert make_config(preset="faster").preset == "faster"
        assert make_config(preset="BAD").preset == "veryfast"
        assert make_config(preset=" MEDIUM ").preset == "medium"
        assert make_config().preset == "veryfast"

    def test_stream_keys_from_string(self):
        cfg = make_config(stream_keys="k1, k2\n\nk3")
        assert cfg.stream_keys == ["k1", "k2", "k3"]

    def test_backup_sources_dedup_and_drop_main(self):
        cfg = make_config(backup_sources="https://example.com/live, https://b.com/1\nhttps://b.com/1, https://b.com/2")
        assert cfg.backup_sources == ["https://b.com/1", "https://b.com/2"]

    def test_notify_webhook_parsed(self):
        assert make_config(notify_webhook=" https://hook.example.com/x ").notify_webhook == "https://hook.example.com/x"


class TestValidation:
    def test_valid_config_passes(self):
        assert make_config().validate() == []

    def test_bad_source_scheme_rejected(self):
        errors = make_config(source_url="ftp://x/live").validate()
        assert any("المصدر" in e for e in errors)

    def test_bad_rtmp_scheme_rejected(self):
        errors = make_config(rtmp_base="http://x/live").validate()
        assert any("RTMP" in e for e in errors)

    def test_bad_webhook_rejected(self):
        errors = make_config(notify_webhook="ftp://hook").validate()
        assert any("الإشعارات" in e for e in errors)

    def test_empty_source_rejected(self):
        assert make_config(source_url="").validate()


class TestHelpers:
    def test_mask_output_url_hides_key(self):
        masked = app.mask_output_url("rtmp://a.rtmp.youtube.com/live2/abcd-1234")
        assert "abcd-1234" not in masked
        assert masked.startswith("rtmp://a.rtmp.youtube.com/live2/")

    def test_headers_value_allowlist(self):
        headers = {"User-Agent": "UA", "Cookie": "c=1", "X-Evil": "no", "Referer": "r"}
        value = app.headers_value(headers)
        assert "User-Agent" in value and "Cookie" in value and "Referer" in value
        assert "X-Evil" not in value

    def test_public_config_hides_keys(self):
        cfg = make_config(stream_keys=["secret1", "secret2"])
        public = app.public_config(cfg)
        assert public["stream_keys"] == []
        assert public["stream_key_count"] == 2
        assert public["has_stream_keys"] is True

    def test_format_scoring_prefers_hls(self):
        hls = {"protocol": "m3u8_native", "height": 720, "fps": 30, "tbr": 2000}
        http = {"protocol": "https", "height": 720, "fps": 30, "tbr": 2000}
        assert app.score_video(hls, 720) > app.score_video(http, 720)

    def test_hls_input_gets_live_edge_start(self):
        hls_args = app.input_options("https://manifest.googlevideo.com/api/manifest/hls_playlist/x/index.m3u8", {})
        assert "-live_start_index" in hls_args
        assert hls_args[hls_args.index("-live_start_index") + 1] == "-3"

    def test_non_hls_input_skips_live_start_index(self):
        args = app.input_options("https://rr1---sn.googlevideo.com/videoplayback?x=1", {})
        assert "-live_start_index" not in args


class TestSourceResolver:
    def test_caches_result(self, monkeypatch):
        calls = []

        def fake_select(config):
            calls.append(1)
            return app.SourceSelection(mode="muxed", video_url="http://x", source_url=config.source_url)

        monkeypatch.setattr(app, "select_source", fake_select)
        resolver = app.SourceResolver(ttl=60)
        cfg = make_config()
        assert resolver.resolve(cfg) is not None
        assert resolver.resolve(cfg) is not None
        assert len(calls) == 1

    def test_invalidate_forces_refresh(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app, "select_source", lambda config: calls.append(1) or app.SourceSelection(mode="muxed", video_url="http://x"))
        resolver = app.SourceResolver(ttl=600)
        cfg = make_config()
        resolver.resolve(cfg)
        resolver.invalidate()
        resolver.resolve(cfg)
        assert len(calls) == 2

    def test_failure_not_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app, "select_source", lambda config: calls.append(1) or None)
        resolver = app.SourceResolver(ttl=600)
        assert resolver.resolve(make_config()) is None
        assert resolver.resolve(make_config()) is None
        assert len(calls) == 2


class TestFfmpegCommand:
    def setup_method(self):
        app.FFMPEG = "/usr/bin/ffmpeg"

    def test_muxed_command(self):
        source = app.SourceSelection(mode="muxed", video_url="http://v", video_headers={"User-Agent": "UA"})
        cmd = app.build_ffmpeg_command(source, "rtmp://out/key", make_config())
        assert cmd[0] == "/usr/bin/ffmpeg"
        assert "libx264" in cmd and "aac" in cmd and "flv" in cmd
        assert cmd[-1] == "rtmp://out/key"
        # الصوت في الوضع المدمج إلزامي (لا `?`) — لا بث صامت صامت
        maps = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-map"]
        assert maps == ["0:v:0", "0:a:0"]
        assert "0:a:0?" not in cmd
        assert "-progress" in cmd and "pipe:1" in cmd
        assert cmd[cmd.index("-max_muxing_queue_size") + 1] == "4096"

    def test_split_command_maps_second_input(self):
        source = app.SourceSelection(mode="split", video_url="http://v", audio_url="http://a")
        cmd = app.build_ffmpeg_command(source, "rtmp://out/key", make_config())
        maps = [cmd[i + 1] for i, token in enumerate(cmd) if token == "-map"]
        assert maps == ["0:v:0", "1:a:0"]

    def test_preset_used_in_command(self):
        source = app.SourceSelection(mode="muxed", video_url="http://v")
        cmd = app.build_ffmpeg_command(source, "rtmp://out/key", make_config(preset="faster"))
        assert cmd[cmd.index("-preset") + 1] == "faster"

    def test_video_only_adds_silent_audio(self):
        source = app.SourceSelection(mode="video-only", video_url="http://v")
        cmd = app.build_ffmpeg_command(source, "rtmp://out/key", make_config())
        joined = " ".join(cmd)
        assert "anullsrc" in joined

    def test_missing_ffmpeg_raises(self):
        old = app.FFMPEG
        app.FFMPEG = None
        try:
            source = app.SourceSelection(mode="muxed", video_url="http://v")
            try:
                app.build_ffmpeg_command(source, "rtmp://out/key", make_config())
                assert False, "يجب أن يفشل بدون ffmpeg"
            except RuntimeError:
                pass
        finally:
            app.FFMPEG = old


class TestNotifier:
    def test_dedup_same_message(self):
        notifier = app.Notifier.__new__(app.Notifier)
        import queue as q
        import threading as t
        notifier._queue = q.Queue()
        notifier._recent = {}
        notifier._lock = t.Lock()
        notifier.notify("started", "رسالة", "http://hook/x")
        notifier.notify("started", "رسالة", "http://hook/x")
        assert notifier._queue.qsize() == 1

    def test_empty_url_ignored(self):
        notifier = app.Notifier.__new__(app.Notifier)
        import queue as q
        import threading as t
        notifier._queue = q.Queue()
        notifier._recent = {}
        notifier._lock = t.Lock()
        notifier.notify("started", "رسالة", "")
        assert notifier._queue.qsize() == 0


class TestManager:
    def test_start_requires_keys(self):
        manager = app.RelayManager(make_config(stream_keys=[]))
        ok, message = manager.start()
        assert not ok and "مفتاح" in message

    def test_start_rejects_invalid_scheme(self):
        manager = app.RelayManager(make_config(rtmp_base="http://bad"))
        ok, message = manager.start()
        assert not ok and "RTMP" in message

    def test_output_urls_built(self):
        manager = app.RelayManager(make_config(stream_keys=["/k1/", "k2"], rtmp_base="rtmp://x/live/"))
        assert manager.output_urls() == ["rtmp://x/live/k1/", "rtmp://x/live/k2"]

    def test_start_one_unknown_worker_rejected(self):
        manager = app.RelayManager(make_config(stream_keys=["k1"]))
        ok, message = manager.start_one("stream-9")
        assert not ok and "غير معروفة" in message
        ok, _ = manager.start_one("garbage")
        assert not ok

    def test_stop_one_when_stopped(self):
        manager = app.RelayManager(make_config(stream_keys=["k1"]))
        ok, message = manager.stop_one("stream-1")
        assert not ok and "متوقفة" in message

    def test_start_one_requires_keys(self):
        manager = app.RelayManager(make_config(stream_keys=[]))
        ok, message = manager.start_one("stream-1")
        assert not ok and "مفتاح" in message

    def test_start_one_validates_config(self):
        manager = app.RelayManager(make_config(stream_keys=["k1"], source_url="ftp://bad"))
        ok, message = manager.start_one("stream-1")
        assert not ok


class TestFormatKindsAndSourceSelection:
    """انحدارات v7: acodec=null لا يعني غياب الصوت + اختيار الفيديو الأنسب للترميز."""

    INFO = {"title": "بث", "is_live": True}

    def test_youtube_audio_only_with_null_acodec_detected(self):
        # yt-dlp يعرض مسارات صوت يوتيوب المباشر (233/234) بـ acodec=None
        fmt = {"format_id": "234", "vcodec": "none", "acodec": None,
               "video_ext": "none", "audio_ext": "mp4", "url": "http://a.m3u8"}
        has_v, has_a = app.format_kinds(fmt)
        assert has_v is False and has_a is True

    def test_youtube_video_only_not_mistaken_for_audio(self):
        fmt = {"format_id": "232", "vcodec": "avc1.4D401F", "acodec": "none",
               "video_ext": "mp4", "audio_ext": "none", "url": "http://v.m3u8"}
        has_v, has_a = app.format_kinds(fmt)
        assert has_v is True and has_a is False

    def test_muxed_with_both_codecs_detected(self):
        fmt = {"vcodec": "avc1", "acodec": "mp4a", "video_ext": "mp4",
               "audio_ext": "mp4", "url": "http://m"}
        has_v, has_a = app.format_kinds(fmt)
        assert has_v and has_a and app.is_muxed(fmt)

    def test_split_chosen_when_audio_only_present(self):
        # السيناريو الحقيقي ليوتيوب المباشر: فيديو منفصل + صوت منفصل acodec=None
        formats = [
            {"format_id": "270", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none",
             "height": 1080, "fps": 30, "tbr": 4000, "video_ext": "mp4", "audio_ext": "none",
             "url": "http://v1080", "http_headers": {}},
            {"format_id": "232", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none",
             "height": 720, "fps": 30, "tbr": 2400, "video_ext": "mp4", "audio_ext": "none",
             "url": "http://v720", "http_headers": {}},
            {"format_id": "234", "protocol": "m3u8_native", "vcodec": "none", "acodec": None,
             "height": None, "video_ext": "none", "audio_ext": "mp4",
             "format_note": "Default, high", "url": "http://a128", "http_headers": {}},
        ]
        cfg = app.RelayConfig.from_dict({"resolution": "1280x720"})
        selection = app.choose_source(formats, self.INFO, cfg)
        assert selection is not None
        assert selection.mode == "split"
        assert selection.audio_url == "http://a128"
        assert selection.video_url == "http://v720"  # يفضل 720p على 1080p لتخفيف CPU

    def test_video_only_fallback_when_no_audio_at_all(self):
        formats = [
            {"format_id": "232", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none",
             "height": 720, "video_ext": "mp4", "audio_ext": "none", "url": "http://v", "http_headers": {}},
        ]
        cfg = app.RelayConfig.from_dict({"resolution": "1280x720"})
        selection = app.choose_source(formats, self.INFO, cfg)
        assert selection is not None and selection.mode == "video-only" and selection.audio_url is None

    def test_muxed_preferred_over_split(self):
        formats = [
            {"format_id": "m1", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "mp4a",
             "height": 720, "video_ext": "mp4", "audio_ext": "mp4", "url": "http://muxed", "http_headers": {}},
            {"format_id": "v1", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none",
             "height": 720, "video_ext": "mp4", "audio_ext": "none", "url": "http://v", "http_headers": {}},
            {"format_id": "a1", "protocol": "m3u8_native", "vcodec": "none", "acodec": None,
             "video_ext": "none", "audio_ext": "mp4", "url": "http://a", "http_headers": {}},
        ]
        cfg = app.RelayConfig.from_dict({"resolution": "1280x720"})
        selection = app.choose_source(formats, self.INFO, cfg)
        assert selection is not None and selection.mode == "muxed"
        assert selection.video_url == "http://muxed"

    def test_muxed_disabled_falls_to_split(self):
        formats = [
            {"format_id": "m1", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "mp4a",
             "height": 720, "video_ext": "mp4", "audio_ext": "mp4", "url": "http://muxed", "http_headers": {}},
            {"format_id": "v1", "protocol": "m3u8_native", "vcodec": "avc1", "acodec": "none",
             "height": 720, "video_ext": "mp4", "audio_ext": "none", "url": "http://v", "http_headers": {}},
            {"format_id": "a1", "protocol": "m3u8_native", "vcodec": "none", "acodec": None,
             "video_ext": "none", "audio_ext": "mp4", "url": "http://a", "http_headers": {}},
        ]
        cfg = app.RelayConfig.from_dict({"resolution": "1280x720"})
        selection = app.choose_source(formats, self.INFO, cfg, allow_muxed=False)
        assert selection is not None and selection.mode == "split"

    def test_pick_best_video_prefers_target_height(self):
        videos = [
            {"height": 1080, "fps": 30, "tbr": 4000},
            {"height": 720, "fps": 30, "tbr": 2400},
            {"height": 480, "fps": 30, "tbr": 1200},
        ]
        assert app.pick_best_video(videos, 720)["height"] == 720

    def test_pick_best_video_lowest_above_when_below_missing(self):
        videos = [
            {"height": 1080, "fps": 30, "tbr": 4000},
            {"height": 720, "fps": 30, "tbr": 2400},
        ]
        assert app.pick_best_video(videos, 360)["height"] == 720


class TestProxyAndCheckHelpers:
    def test_parse_proxy_accepts_http_https(self):
        assert app.parse_proxy(" http://user:pass@proxy.local:3128 ") == "http://user:pass@proxy.local:3128"
        assert app.parse_proxy("https://p.local:8443") == "https://p.local:8443"

    def test_parse_proxy_rejects_other_schemes_and_garbage(self):
        assert app.parse_proxy("socks5://x:1080") == ""
        assert app.parse_proxy("ftp://x") == ""
        assert app.parse_proxy("not-a-url") == ""
        assert app.parse_proxy("") == ""
        assert app.parse_proxy(None) == ""

    def test_config_proxy_normalized(self):
        assert make_config(proxy=" http://h:1 ").proxy == "http://h:1"
        assert make_config(proxy="socks5://h:1").proxy == ""
        assert make_config().proxy == ""

    def test_mask_proxy_hides_credentials(self):
        assert app.mask_proxy("http://user:secret@host:3128") == "http://•••:•••@host:3128"
        assert app.mask_proxy("http://host:3128") == "http://host:3128"
        assert app.mask_proxy("") == ""

    def test_public_config_masks_proxy_credentials(self):
        public = app.public_config(make_config(proxy="http://user:secret@host:3128"))
        assert "secret" not in public["proxy"]
        assert public["has_proxy"] is True
        public2 = app.public_config(make_config())
        assert public2["proxy"] == "" and public2["has_proxy"] is False

    def test_proxy_env_set_only_with_proxy(self):
        assert app.proxy_env(make_config()) is None
        env = app.proxy_env(make_config(proxy="http://h:3128"))
        assert env["http_proxy"] == "http://h:3128"
        assert env["https_proxy"] == "http://h:3128"
        assert "no_proxy" in env

    def test_rtmp_reachable_local(self):
        import socket as s
        listener = s.socket(s.AF_INET, s.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            ok, msg = app.check_rtmp_reachable(f"rtmp://127.0.0.1:{port}/live2", timeout=3)
            assert ok, msg
        finally:
            listener.close()
        ok2, _ = app.check_rtmp_reachable("rtmp://127.0.0.1:1/live2", timeout=2)
        assert not ok2

    def test_probe_media_flow_dead_url_returns_zero(self):
        app.FFMPEG = app.resolve_ffmpeg()
        source = app.SourceSelection(mode="video-only", video_url="http://127.0.0.1:9/dead.m3u8")
        result = app.probe_media_flow(source, make_config(), seconds=3)
        assert result["ran"] is True
        assert result["frames"] == 0


class TestPanelTokenAndLogLevel:
    def test_panel_token_parsed(self):
        cfg = make_config(panel_token=" secret123 ")
        assert cfg.panel_token == "secret123"

    def test_panel_token_hidden_from_public_config(self):
        cfg = make_config(panel_token="secret123")
        public = app.public_config(cfg)
        assert "panel_token" not in public
        assert public["has_panel_token"] is True

    def test_no_token_flag_false(self):
        old = app.PANEL_TOKEN
        app.PANEL_TOKEN = ""
        try:
            public = app.public_config(make_config())
            assert public["has_panel_token"] is False
        finally:
            app.PANEL_TOKEN = old

    def test_log_level_validated(self):
        assert make_config(log_level="debug").log_level == "DEBUG"
        assert make_config(log_level="verbose").log_level == "INFO"
        assert make_config(log_level="WARNING").log_level == "WARNING"
