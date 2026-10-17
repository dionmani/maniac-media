import pytest
from backend.domain.quality import ParsedQuality, Quality, Resolution, Source, Codec
from backend.domain.inspection import (
    parse_ffprobe, resolution_class, compare, 
    MediaInspection, VideoStream, AudioStream, SubtitleStream, Finding, normalize_language
)
from backend.domain import naming

def test_resolution_class_table():
    assert resolution_class(3840, 2160) == Resolution.R2160
    assert resolution_class(3840, 1608) == Resolution.R2160
    assert resolution_class(1920, 1080) == Resolution.R1080
    assert resolution_class(1920, 800) == Resolution.R1080
    assert resolution_class(1280, 720) == Resolution.R720
    assert resolution_class(1280, 536) == Resolution.R720
    assert resolution_class(720, 576) == Resolution.SD
    assert resolution_class(640, 480) == Resolution.SD
    assert resolution_class(0, 0) == Resolution.UNKNOWN

def test_parse_ffprobe_hevc_dv_hdr10_4k():
    payload = {
        "format": {
            "format_name": "matroska,webm",
            "duration": "7200.5",
            "bit_rate": "15000000"
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "color_transfer": "smpte2084",
                "codec_tag_string": "dvhe",
                "side_data_list": [
                    {"side_data_type": "DOVI configuration record"},
                ]
            },
            {
                "codec_type": "audio",
                "codec_name": "truehd",
                "channels": 8,
                "channel_layout": "7.1",
                "tags": {"language": "eng", "title": "Dolby Atmos 7.1"}
            }
        ]
    }
    result = parse_ffprobe(payload)
    assert result.container == "matroska"
    assert result.duration_seconds == 7200.5
    assert result.video.codec == "hevc"
    assert result.video.width == 3840
    assert result.video.height == 2160
    assert result.video.hdr_format == "DV HDR10"
    assert result.audio[0].codec == "truehd"
    assert result.audio[0].profile == "Atmos"
    assert result.audio[0].channels == 8

def test_parse_ffprobe_1080p_scope():
    payload = {
        "format": {"format_name": "matroska", "duration": "5400", "bit_rate": "8000000"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 800},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "en"}},
        ]
    }
    result = parse_ffprobe(payload)
    assert result.video.width == 1920
    assert result.video.height == 800
    assert resolution_class(result.video.width, result.video.height) == Resolution.R1080
    assert result.video.hdr_format is None  # SDR

def test_hdr_detection_matrix():
    # Dolby Vision alone
    p_dv = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "side_data_list": [{"side_data_type": "DOVI configuration record"}]}]}
    assert parse_ffprobe(p_dv).video.hdr_format == "DV"

    # HDR10+ alone
    p_hdr10plus = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "side_data_list": [{"side_data_type": "SMPTE 2094-40"}]}]}
    assert parse_ffprobe(p_hdr10plus).video.hdr_format == "HDR10+"

    # HDR10 alone (smpte2084 color transfer)
    p_hdr10 = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "color_transfer": "smpte2084"}]}
    assert parse_ffprobe(p_hdr10).video.hdr_format == "HDR10"

    # HLG alone (arib-std-b67 color transfer)
    p_hlg = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "color_transfer": "arib-std-b67"}]}
    assert parse_ffprobe(p_hlg).video.hdr_format == "HLG"

    # DV + HDR10+
    p_dv_hdr10plus = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160, "codec_tag_string": "dvh1", "side_data_list": [{"side_data_type": "SMPTE 2094-40"}]}]}
    assert parse_ffprobe(p_dv_hdr10plus).video.hdr_format == "DV HDR10+"

    # SDR
    p_sdr = {"format": {}, "streams": [{"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080, "color_transfer": "bt709"}]}
    assert parse_ffprobe(p_sdr).video.hdr_format is None

def test_parse_multi_audio_with_commentary():
    payload = {
        "format": {},
        "streams": [
            {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080},
            {"codec_type": "audio", "codec_name": "eac3", "channels": 6, "tags": {"language": "eng"}, "disposition": {"default": 1}},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "tags": {"language": "eng", "title": "Director Commentary"}, "disposition": {"comment": 1}},
        ]
    }
    result = parse_ffprobe(payload)
    assert len(result.audio) == 2
    assert result.audio[0].default is True
    assert result.audio[0].commentary is False
    assert result.audio[1].commentary is True

def test_parse_forced_and_sdh_subtitles():
    payload = {
        "format": {},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
            {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng"}, "disposition": {"forced": 1}},
            {"codec_type": "subtitle", "codec_name": "subrip", "tags": {"language": "eng", "title": "English SDH"}, "disposition": {"hearing_impaired": 1}},
        ]
    }
    result = parse_ffprobe(payload)
    assert len(result.subtitles) == 2
    assert result.subtitles[0].forced is True
    assert result.subtitles[0].hearing_impaired is False
    assert result.subtitles[1].forced is False
    assert result.subtitles[1].hearing_impaired is True

def test_parse_no_audio():
    payload = {
        "format": {},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080},
        ]
    }
    result = parse_ffprobe(payload)
    assert len(result.audio) == 0

    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    findings = compare(claimed, result, expected_runtime_min=100, preferred_audio_languages=[], is_movie=True)
    assert any(f.kind == "missing_audio" and f.severity == "error" for f in findings)

def test_compare_quality_mismatch():
    claimed = ParsedQuality(Quality("HDTV-1080p", Resolution.R1080, Source.HDTV, 70), Codec.H264)
    actual = MediaInspection("mp4", 100, 1000, VideoStream("h264", 1280, 720, None, None, None, None, None), (), (), 0)
    
    findings = compare(claimed, actual, expected_runtime_min=None, preferred_audio_languages=[], is_movie=False)
    assert any(f.kind == "quality_mismatch" and f.severity == "warning" for f in findings)
    assert any(f.kind == "missing_audio" for f in findings)

def test_compare_runtime_tolerance_edges():
    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    
    # movie default tolerance 20%
    # expected 100 min: 80 min is exactly 20% delta -> within tolerance
    actual_80 = MediaInspection("mp4", 80 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "eng", None, True, False),), (), 0)
    assert compare(claimed, actual_80, expected_runtime_min=100, preferred_audio_languages=[], runtime_tolerance_movie_pct=20, is_movie=True) == []

    # 75 min is 25% delta -> exceeds 20% tolerance -> runtime_mismatch
    actual_75 = MediaInspection("mp4", 75 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "eng", None, True, False),), (), 0)
    findings = compare(claimed, actual_75, expected_runtime_min=100, preferred_audio_languages=[], runtime_tolerance_movie_pct=20, is_movie=True)
    assert len(findings) == 1 and findings[0].kind == "runtime_mismatch"

    # severely short: expected 100 min, actual 15 min, and only 200 MB for a 1080p film -> truncated
    actual_15 = MediaInspection("mp4", 15 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "eng", None, True, False),), (), 0)
    findings_trunc = compare(claimed, actual_15, expected_runtime_min=100, preferred_audio_languages=[], is_movie=True,
                             file_size_bytes=200 * 1024 * 1024)
    assert len(findings_trunc) == 1 and findings_trunc[0].kind == "truncated_file" and findings_trunc[0].severity == "error"


def _short_1080p(minutes: float) -> MediaInspection:
    return MediaInspection("mkv", minutes * 60, None, VideoStream("h264", 1920, 1080, None, None, None, None, None),
                           (AudioStream("aac", None, 2, None, "eng", None, True, False),), (), 0)


def test_truncation_requires_a_small_file_not_just_a_short_duration():
    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    # a 22-min episode at a normal bitrate (~4 Mbps, 660 MB) against a noisy 60-min series runtime:
    # wrong runtime metadata or wrong episode -> runtime_mismatch warning, never an error-level truncation
    normal = compare(claimed, _short_1080p(22), expected_runtime_min=60, preferred_audio_languages=[],
                     file_size_bytes=660 * 1024 * 1024)
    assert [f.kind for f in normal] == ["runtime_mismatch"] and normal[0].severity == "warning"

    # same duration but only 120 MB, far below a complete 60-min 1080p file -> truncated_file
    tiny = compare(claimed, _short_1080p(22), expected_runtime_min=60, preferred_audio_languages=[],
                   file_size_bytes=120 * 1024 * 1024)
    assert [f.kind for f in tiny] == ["truncated_file"]
    assert tiny[0].data["size_bytes"] == 120 * 1024 * 1024 and tiny[0].data["expected_min_size_bytes"] > 0

    # unknown size -> no truncation verdict, falls back to the runtime check
    assert [f.kind for f in compare(claimed, _short_1080p(22), expected_runtime_min=60, preferred_audio_languages=[])] == ["runtime_mismatch"]

    # small but not short enough (> 50 % of expected) -> not truncated
    assert [f.kind for f in compare(claimed, _short_1080p(40), expected_runtime_min=60, preferred_audio_languages=[],
                                    file_size_bytes=50 * 1024 * 1024)] == ["runtime_mismatch"]


def test_no_video_stream_is_invalid_media_and_still_checks_audio():
    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    findings = compare(claimed, MediaInspection("mkv", 60.0, None, None, (), (), 0), expected_runtime_min=None, preferred_audio_languages=[])
    assert {f.kind for f in findings} == {"invalid_media", "missing_audio"}


def test_inspection_issue_types_are_registered_and_not_corrupt_file():
    from backend.domain.issues import IssueType, spec_for
    from backend.services.inspection_service import INSPECTION_ISSUE_TYPES

    assert "corrupt_file" not in INSPECTION_ISSUE_TYPES  # owned by the scanner's header check
    assert all(spec_for(t) is not None for t in INSPECTION_ISSUE_TYPES)
    assert spec_for(IssueType.TRUNCATED_FILE).severity == "error"

def test_language_normalization():
    assert normalize_language("eng") == "eng"
    assert normalize_language("en") == "eng"
    assert normalize_language("fre") == "fra"
    assert normalize_language("fr") == "fra"
    assert normalize_language("de") == "deu"
    assert normalize_language("es") == "spa"

    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    # File has audio language "en", preferred is ["eng"] -> normalized match, no finding
    actual_en = MediaInspection("mp4", 60 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "en", None, True, False),), (), 0)
    assert compare(claimed, actual_en, expected_runtime_min=60, preferred_audio_languages=["eng"], is_movie=False) == []

def test_naming_mediainfo_tokens():
    template = "{Title} ({Year}) [{mediainfo_videocodec} {mediainfo_hdr} {mediainfo_audiochannels}]"
    errors = naming.validate(template, naming.MOVIE_TOKENS)
    assert errors == []
    assert naming.uses_mediainfo_tokens(template) is True

    # preview without inspect_before_import yields warnings
    p_warn = naming.preview(template, "movie", inspect_before_import=False)
    assert len(p_warn.warnings) > 0
    assert "MediaInfo tokens" in p_warn.warnings[0]

    # preview with inspect_before_import has no warnings
    p_ok = naming.preview(template, "movie", inspect_before_import=True)
    assert p_ok.warnings == []


# --------------------------------------------------------------------------- milestone 7
from backend.domain.inspection import (  # noqa: E402
    DecodePlan,
    classify_decode,
    decode_plan,
    format_mediainfo,
    mediainfo_tokens,
)


def test_decode_plan_samples_start_middle_and_end_of_long_files():
    assert decode_plan(7200) == DecodePlan("0%+10,3600%+10,7185%+10", 30.0)
    assert decode_plan(30) == DecodePlan("0%+30", 30.0)       # short file: decode all of it
    assert decode_plan(None) == DecodePlan("0%+30", 30.0)     # unknown duration: the first 30 s


def test_classify_decode():
    plan = decode_plan(600)
    assert classify_decode("", 708, plan, 24.0) is None
    assert classify_decode("[h264] error while decoding MB 1 2\n", 720, plan, 24.0) is None  # one benign-looking line
    short = classify_decode("[matroska,webm] File ended prematurely\n", 79, plan, 24.0)
    assert short.kind == "truncated_file" and short.data["reason"] == "decode_short_read" and short.data["frames_expected"] == 720
    assert classify_decode("", 100, plan, 24.0).kind == "truncated_file"  # too few frames even without stderr
    noisy = classify_decode("[h264] error while decoding MB\n" * 5, 720, plan, 24.0)
    assert noisy.kind == "invalid_media" and noisy.data["reason"] == "decode_errors" and noisy.data["source"] == "decode"
    assert classify_decode("x: Invalid data found when processing input\n", None, plan, None).kind == "invalid_media"


def test_mediainfo_token_formatting():
    assert format_mediainfo("hevc", "truehd", 8, "DV HDR10", "2160p") == {
        "mediainfo_videocodec": "x265", "mediainfo_audiocodec": "TrueHD", "mediainfo_audiochannels": "7.1",
        "mediainfo_hdr": "DV HDR10", "mediainfo_resolution": "2160p"}
    assert format_mediainfo("vp9", None, None, None, None)["mediainfo_videocodec"] == "VP9"
    assert all(v == "" for v in mediainfo_tokens(None).values())
    inspection = MediaInspection("mkv", 60.0, None, VideoStream("h264", 1920, 800, None, None, None, None, None),
                                 (AudioStream("aac", None, 2, None, "eng", None, False, False),
                                  AudioStream("eac3", None, 6, None, "eng", None, True, False)), (), 0)
    tokens = mediainfo_tokens(inspection)
    assert tokens["mediainfo_audiocodec"] == "EAC3" and tokens["mediainfo_audiochannels"] == "5.1"  # default track wins
    assert tokens["mediainfo_resolution"] == "1080p"  # scope crop still classifies by width
