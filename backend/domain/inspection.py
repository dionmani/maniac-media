from dataclasses import dataclass
from typing import Any

from backend.domain.quality import ParsedQuality, Resolution


class InspectionParseError(Exception):
    pass


@dataclass(frozen=True)
class VideoStream:
    codec: str            # hevc | h264 | av1 | mpeg2video | vc1 ...
    width: int
    height: int
    bit_depth: int | None
    frame_rate: float | None
    bitrate: int | None
    hdr_format: str | None  # "DV", "DV HDR10", "DV HDR10+", "HDR10+", "HDR10", "HLG", None
    profile: str | None


@dataclass(frozen=True)
class AudioStream:
    codec: str            # truehd | eac3 | ac3 | dts | aac | flac | opus ...
    profile: str | None   # "DTS-HD MA", "Atmos" (from profile/title)
    channels: int
    layout: str | None    # "7.1", "5.1(side)", "stereo"
    language: str | None  # ISO 639-2 as tagged; "und" -> None
    title: str | None
    default: bool
    commentary: bool      # disposition.comment or title matches /commentary/i


@dataclass(frozen=True)
class SubtitleStream:
    codec: str            # subrip | ass | hdmv_pgs_subtitle | dvd_subtitle
    language: str | None
    forced: bool
    hearing_impaired: bool
    default: bool


@dataclass(frozen=True)
class MediaInspection:
    container: str
    duration_seconds: float | None
    overall_bitrate: int | None
    video: VideoStream | None
    audio: tuple[AudioStream, ...]
    subtitles: tuple[SubtitleStream, ...]
    chapters: int


def resolution_class(width: int, height: int) -> Resolution:
    if width <= 0 or height <= 0:
        return Resolution.UNKNOWN
    if width >= 3200 or height >= 1800:
        return Resolution.R2160
    if width >= 1700 or height >= 900:
        return Resolution.R1080
    if width >= 1200 or height >= 600:
        return Resolution.R720
    return Resolution.SD


def parse_ffprobe(payload: dict[str, Any]) -> MediaInspection:
    try:
        format_info = payload.get("format", {})
        streams = payload.get("streams", [])
        chapters = payload.get("chapters", [])
        
        container = format_info.get("format_name", "unknown").split(",")[0]
        duration_seconds = float(format_info.get("duration")) if format_info.get("duration") is not None else None
        overall_bitrate = int(format_info.get("bit_rate")) if format_info.get("bit_rate") is not None else None

        video = None
        audio = []
        subtitles = []

        for stream in streams:
            codec_type = stream.get("codec_type")
            if codec_type == "video" and video is None and stream.get("codec_name") not in ("mjpeg", "png", "bmp"):
                # Handle HDR
                hdr_format = None
                color_transfer = stream.get("color_transfer")
                side_data = stream.get("side_data_list", [])
                
                has_dv = stream.get("codec_tag_string") in ("dvhe", "dvh1") or stream.get("codec_tag") in ("dvhe", "dvh1")
                has_hdr10 = color_transfer == "smpte2084"
                has_hdr10plus = False
                
                for sd in side_data:
                    sd_type = sd.get("side_data_type", "")
                    if "DOVI" in sd_type or sd_type == "DOVI configuration record":
                        has_dv = True
                    if "2094-40" in sd_type or sd_type in ("SMPTE 2094-40", "SMPTE2094-40"):
                        has_hdr10plus = True

                if has_dv and has_hdr10plus:
                    hdr_format = "DV HDR10+"
                elif has_dv and has_hdr10:
                    hdr_format = "DV HDR10"
                elif has_dv:
                    hdr_format = "DV"
                elif has_hdr10plus:
                    hdr_format = "HDR10+"
                elif has_hdr10:
                    hdr_format = "HDR10"
                elif color_transfer == "arib-std-b67":
                    hdr_format = "HLG"

                # Framerate
                frame_rate = None
                avg_frame_rate = stream.get("avg_frame_rate", "0/0")
                if "/" in avg_frame_rate:
                    num, den = avg_frame_rate.split("/")
                    if int(den) > 0:
                        frame_rate = round(int(num) / int(den), 3)
                
                bit_depth = None
                bits_per_raw_sample = stream.get("bits_per_raw_sample")
                if bits_per_raw_sample and str(bits_per_raw_sample).isdigit():
                    bit_depth = int(bits_per_raw_sample)
                else:
                    pix_fmt = stream.get("pix_fmt", "")
                    if "10" in pix_fmt:
                        bit_depth = 10
                    elif "12" in pix_fmt:
                        bit_depth = 12
                    elif "8" in pix_fmt:
                        bit_depth = 8

                v_bitrate = int(stream.get("bit_rate")) if stream.get("bit_rate") is not None else None

                video = VideoStream(
                    codec=stream.get("codec_name", ""),
                    width=stream.get("width", 0),
                    height=stream.get("height", 0),
                    bit_depth=bit_depth,
                    frame_rate=frame_rate,
                    bitrate=v_bitrate,
                    hdr_format=hdr_format,
                    profile=stream.get("profile")
                )

            elif codec_type == "audio":
                tags = stream.get("tags", {})
                disposition = stream.get("disposition", {})
                
                lang = tags.get("language")
                if lang and lang.lower() in ("und", "unk"):
                    lang = None
                    
                title = tags.get("title")
                profile = stream.get("profile")
                if title and "atmos" in title.lower():
                    if not profile:
                        profile = "Atmos"
                    elif "atmos" not in profile.lower():
                        profile = f"{profile} Atmos"
                commentary = bool(disposition.get("comment", 0)) or (title is not None and "commentary" in title.lower())
                
                audio.append(AudioStream(
                    codec=stream.get("codec_name", ""),
                    profile=profile,
                    channels=stream.get("channels", 2),
                    layout=stream.get("channel_layout"),
                    language=lang,
                    title=title,
                    default=bool(disposition.get("default", 0)),
                    commentary=commentary
                ))
            
            elif codec_type == "subtitle":
                tags = stream.get("tags", {})
                disposition = stream.get("disposition", {})
                
                lang = tags.get("language")
                if lang and lang.lower() in ("und", "unk"):
                    lang = None
                sub_title = (tags.get("title") or "").lower()
                forced = bool(disposition.get("forced", 0)) or "forced" in sub_title
                sdh = bool(disposition.get("hearing_impaired", 0)) or any(w in sub_title for w in ("sdh", "hearing impaired"))

                subtitles.append(SubtitleStream(
                    codec=stream.get("codec_name", ""),
                    language=lang,
                    forced=forced,
                    hearing_impaired=sdh,
                    default=bool(disposition.get("default", 0))
                ))

        return MediaInspection(
            container=container,
            duration_seconds=duration_seconds,
            overall_bitrate=overall_bitrate,
            video=video,
            audio=tuple(audio),
            subtitles=tuple(subtitles),
            chapters=len(chapters)
        )
    except Exception as e:
        raise InspectionParseError(f"Failed to parse ffprobe output: {e}") from e


_LANG_ALIASES: dict[str, str] = {
    "en": "eng", "english": "eng",
    "es": "spa", "spanish": "spa",
    "fr": "fra", "fre": "fra", "french": "fra",
    "de": "deu", "ger": "deu", "german": "deu",
    "it": "ita", "italian": "ita",
    "pt": "por", "portuguese": "por",
    "ja": "jpn", "japanese": "jpn",
    "ko": "kor", "korean": "kor",
    "zh": "zho", "chi": "zho", "chinese": "zho",
    "ru": "rus", "russian": "rus",
    "nl": "nld", "dut": "nld", "dutch": "nld",
    "sv": "swe", "no": "nor", "da": "dan", "fi": "fin",
    "pl": "pol", "cs": "ces", "cze": "ces", "hu": "hun",
    "tr": "tur", "ar": "ara", "hi": "hin", "th": "tha",
    "vi": "vie", "id": "ind", "el": "ell", "gre": "ell",
    "ro": "ron", "rum": "ron", "sk": "slk", "slo": "slk",
    "he": "heb", "uk": "ukr",
}


def normalize_language(code: str | None) -> str | None:
    if not code:
        return None
    cleaned = code.strip().lower()
    if cleaned in ("und", "unk", ""):
        return None
    return _LANG_ALIASES.get(cleaned, cleaned)


# Low-end typical video bitrate per *measured* resolution class (bits/s). A file is only called
# truncated when it is far below what even a lean encode would need for the expected runtime; a short
# file at a normal bitrate is more likely the wrong episode/cut and is reported as runtime_mismatch.
_BITRATE_FLOOR: dict[Resolution, int] = {
    Resolution.SD: 500_000,
    Resolution.R720: 1_000_000,
    Resolution.R1080: 2_000_000,
    Resolution.R2160: 6_000_000,
}
TRUNCATED_DURATION_RATIO = 0.5  # duration below this share of the expected runtime ...
TRUNCATED_SIZE_RATIO = 0.5      # ... and size below this share of floor bitrate x expected runtime


def expected_min_size(resolution: Resolution, expected_runtime_min: int) -> int | None:
    """Smallest plausible size in bytes for a complete file of this resolution and runtime."""
    floor = _BITRATE_FLOOR.get(resolution)
    return None if floor is None else floor * expected_runtime_min * 60 // 8


@dataclass(frozen=True)
class Finding:
    kind: str       # quality_mismatch | runtime_mismatch | missing_audio | language_missing | truncated_file | invalid_media
    severity: str
    message: str
    data: dict[str, Any]


_RES_RANK = {Resolution.UNKNOWN: 0, Resolution.SD: 1, Resolution.R720: 2, Resolution.R1080: 3, Resolution.R2160: 4}


def compare(
    claimed: ParsedQuality,
    actual: MediaInspection,
    *,
    expected_runtime_min: int | None,
    preferred_audio_languages: list[str],
    is_movie: bool = False,
    runtime_tolerance_episode_pct: int = 25,
    runtime_tolerance_movie_pct: int = 15,
    file_size_bytes: int | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    actual_res = Resolution.UNKNOWN

    if not actual.video:
        findings.append(Finding("invalid_media", "error", "No video stream found", {"reason": "no_video_stream"}))
    else:
        actual_res = resolution_class(actual.video.width, actual.video.height)
        claimed_res = claimed.quality.resolution
        if claimed_res != Resolution.UNKNOWN and actual_res != Resolution.UNKNOWN and actual_res != claimed_res:
            lower = _RES_RANK[actual_res] < _RES_RANK[claimed_res]
            dims = f"{actual.video.width}×{actual.video.height}"
            findings.append(Finding(
                kind="quality_mismatch",
                severity="warning" if lower else "info",
                message=(f"Claimed quality is {claimed_res.value} but actual resolution is {actual_res.value} ({dims})" if lower
                         else f"Actual resolution ({actual_res.value}, {dims}) is higher than claimed ({claimed_res.value})"),
                data={"claimed": claimed_res.value, "actual": actual_res.value, "width": actual.video.width, "height": actual.video.height},
            ))

    findings.extend(_runtime_findings(actual, actual_res, expected_runtime_min, is_movie, file_size_bytes,
                                      runtime_tolerance_movie_pct if is_movie else runtime_tolerance_episode_pct))
    findings.extend(_audio_findings(actual, preferred_audio_languages))
    return findings


def _runtime_findings(actual: MediaInspection, actual_res: Resolution, expected_min: int | None, is_movie: bool,
                      file_size_bytes: int | None, tolerance_pct: int) -> list[Finding]:
    if actual.duration_seconds is None or not expected_min or expected_min <= 0:
        return []
    if not is_movie and expected_min < 5:  # very short episodes: runtime metadata is too noisy
        return []
    actual_min = actual.duration_seconds / 60
    data = {"actual_min": round(actual_min, 1), "expected_min": expected_min}

    # Truncation needs both signals (spec §4.1): far too short *and* far too small for its resolution.
    # Duration alone is ambiguous – series-level runtime fallbacks are often wrong by 2-3x.
    if actual_min < expected_min * TRUNCATED_DURATION_RATIO and file_size_bytes:
        min_size = expected_min_size(actual_res, expected_min)
        if min_size and file_size_bytes < min_size * TRUNCATED_SIZE_RATIO:
            return [Finding(
                kind="truncated_file",
                severity="error",
                message=(f"File appears truncated: {actual_min:.1f} min of an expected {expected_min} min, "
                         f"and {file_size_bytes / 1_048_576:.0f} MB is far below a complete {actual_res.value} file"),
                data={**data, "size_bytes": file_size_bytes, "expected_min_size_bytes": min_size},
            )]

    tolerance = tolerance_pct / 100.0
    if is_movie:
        # extended cuts are legitimately longer: only shorter counts, unless wildly longer (> +40 %)
        mismatch = actual_min < expected_min * (1 - tolerance) or actual_min > expected_min * 1.40
    else:
        mismatch = abs(actual_min - expected_min) / expected_min > tolerance
    if not mismatch:
        return []
    return [Finding("runtime_mismatch", "warning", f"Runtime deviation: actual {actual_min:.1f} min vs expected {expected_min} min", data)]


def _audio_findings(actual: MediaInspection, preferred_audio_languages: list[str]) -> list[Finding]:
    if not actual.audio:
        return [Finding("missing_audio", "error", "No audio streams found in file", {})]
    if not preferred_audio_languages:
        return []
    audio_langs_raw = [a.language for a in actual.audio if a.language]
    norm_audio = {n for n in map(normalize_language, audio_langs_raw) if n}
    norm_preferred = {n for n in map(normalize_language, preferred_audio_languages) if n}
    # untagged ("und") audio is unknown, never missing
    if not norm_audio or not norm_preferred or norm_audio & norm_preferred:
        return []
    return [Finding(
        kind="language_missing",
        severity="warning",
        message=f"None of the preferred audio languages ({', '.join(preferred_audio_languages)}) found",
        data={"found_languages": sorted(set(audio_langs_raw)), "preferred_languages": preferred_audio_languages},
    )]
