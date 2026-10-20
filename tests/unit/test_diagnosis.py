import errno

import pytest

from backend.domain.diagnosis import (
    SIGNATURE_MAX_LEN,
    ImportFailureAnalysis,
    ImportFailureReason,
    classify_oserror,
    infer_path_mapping,
)


@pytest.mark.parametrize(
    "reported,candidates,expected",
    [
        ("/downloads/complete/X", ["D:\\Downloads\\complete\\X"], ("/downloads", "D:\\Downloads")),
        ("/data/torrents/tv/X", ["/mnt/user/data/torrents/tv/X"], ("/", "/mnt/user")),
        ("\\\\nas\\downloads\\X", ["D:\\media\\downloads\\X"], ("\\\\nas", "D:\\media")),
        ("/downloads/X", ["/library/movies/Y"], None),
        ("/downloads/X", [], None),
    ],
)
def test_infer_path_mapping(reported, candidates, expected):
    assert infer_path_mapping(reported, candidates) == expected


def test_infer_path_mapping_picks_the_longest_match_among_candidates():
    reported = "/downloads/complete/Show/S01/episode.mkv"
    candidates = [
        "/mnt/other/episode.mkv",
        "/mnt/media/Show/S01/episode.mkv",
    ]
    assert infer_path_mapping(reported, candidates) == ("/downloads/complete", "/mnt/media")


@pytest.mark.parametrize(
    "errno_,winerror,expected",
    [
        (errno.EACCES, None, ImportFailureReason.PERMISSION_DENIED),
        (errno.EPERM, None, ImportFailureReason.PERMISSION_DENIED),
        (errno.ENOSPC, None, ImportFailureReason.DISK_FULL),
        (errno.EXDEV, None, ImportFailureReason.CROSS_DEVICE_FAILED),
        (errno.ENAMETOOLONG, None, ImportFailureReason.PATH_TOO_LONG),
        (None, 206, ImportFailureReason.PATH_TOO_LONG),
        (errno.EEXIST, None, ImportFailureReason.DESTINATION_EXISTS),
        (errno.EIO, None, ImportFailureReason.UNKNOWN),
        (None, None, ImportFailureReason.UNKNOWN),
    ],
)
def test_classify_oserror(errno_, winerror, expected):
    assert classify_oserror(errno_, winerror) == expected


def _analysis(signature: str) -> ImportFailureAnalysis:
    return ImportFailureAnalysis(reason=ImportFailureReason.PATH_NOT_FOUND, summary="", evidence={}, fixes=(),
                                 signature=signature, confidence=0.5, analysed_at="")


def test_long_signature_fits_the_column_and_still_groups():
    path = "path_not_found:/data/downloads/complete/" + "the.simpsons.s09e10.miracle.on.evergreen.terrace." * 4 + "mkv"
    a, b, other = _analysis(path), _analysis(path), _analysis(path + "x")
    assert len(a.signature) <= SIGNATURE_MAX_LEN
    assert a.signature == b.signature != other.signature
    assert a.signature.startswith("path_not_found:/data/downloads/complete/")
    assert _analysis("unknown").signature == "unknown"
