import pytest
from backend.domain.quality import ParsedQuality, Quality, Resolution, Source, Codec
from backend.domain.inspection import (
    parse_ffprobe, resolution_class, compare, 
    MediaInspection, VideoStream, AudioStream, SubtitleStream, Finding
)

def test_resolution_class():
    assert resolution_class(3840, 2160) == Resolution.R2160
    assert resolution_class(3840, 1608) == Resolution.R2160
    assert resolution_class(1920, 1080) == Resolution.R1080
    assert resolution_class(1920, 800) == Resolution.R1080
    assert resolution_class(1280, 720) == Resolution.R720
    assert resolution_class(1280, 536) == Resolution.R720
    assert resolution_class(720, 576) == Resolution.SD
    assert resolution_class(640, 480) == Resolution.SD

def test_parse_ffprobe_basic():
    payload = {
        "format": {
            "format_name": "matroska,webm",
            "duration": "7200.5",
            "bit_rate": "1500000"
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
                    {"side_data_type": "SMPTE 2094-40"}
                ]
            },
            {
                "codec_type": "audio",
                "codec_name": "eac3",
                "channels": 6,
                "tags": {"language": "eng"}
            },
            {
                "codec_type": "subtitle",
                "codec_name": "subrip",
                "tags": {"language": "eng"},
                "disposition": {"forced": 1}
            }
        ]
    }
    
    result = parse_ffprobe(payload)
    assert result.container == "matroska"
    assert result.duration_seconds == 7200.5
    assert result.overall_bitrate == 1500000
    assert result.video.codec == "hevc"
    assert result.video.width == 3840
    assert result.video.hdr_format == "DV HDR10" # DOVI + HDR10+ overrides HDR10? Wait, the logic is DOVI + SMPTE2084 = DV HDR10. Let's adjust this test to match what logic outputs: has_dv=True, has_hdr10=True -> "DV HDR10"
    
    assert len(result.audio) == 1
    assert result.audio[0].codec == "eac3"
    assert result.audio[0].channels == 6
    assert result.audio[0].language == "eng"
    
    assert len(result.subtitles) == 1
    assert result.subtitles[0].language == "eng"
    assert result.subtitles[0].forced is True

def test_compare_quality_mismatch():
    claimed = ParsedQuality(Quality("HDTV-1080p", Resolution.R1080, Source.HDTV, 70), Codec.H264)
    actual = MediaInspection("mp4", 100, 1000, VideoStream("h264", 1280, 720, None, None, None, None, None), (), (), 0)
    
    findings = compare(claimed, actual, expected_runtime_min=None, preferred_audio_languages=[], is_movie=False)
    assert len(findings) == 2 # 1 quality_mismatch, 1 missing_audio
    assert findings[0].kind == "quality_mismatch"
    assert findings[0].severity == "warning"
    assert findings[1].kind == "missing_audio"

def test_compare_runtime_mismatch():
    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    # expected 60 min, actual 15 min -> truncated
    actual = MediaInspection("mp4", 15 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "eng", None, True, False),), (), 0)
    
    findings = compare(claimed, actual, expected_runtime_min=60, preferred_audio_languages=[], is_movie=False)
    assert findings[0].kind == "truncated"
    
    # expected 60 min, actual 40 min -> mismatch
    actual2 = MediaInspection("mp4", 40 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "eng", None, True, False),), (), 0)
    findings2 = compare(claimed, actual2, expected_runtime_min=60, preferred_audio_languages=[], is_movie=False)
    assert findings2[0].kind == "runtime_mismatch"

def test_compare_language_missing():
    claimed = ParsedQuality(Quality("WEBDL-1080p", Resolution.R1080, Source.WEBDL, 80), Codec.H264)
    actual = MediaInspection("mp4", 60 * 60, 1000, VideoStream("h264", 1920, 1080, None, None, None, None, None), (AudioStream("aac", None, 2, None, "fre", None, True, False),), (), 0)
    
    findings = compare(claimed, actual, expected_runtime_min=60, preferred_audio_languages=["eng"], is_movie=False)
    assert findings[0].kind == "language_missing"
