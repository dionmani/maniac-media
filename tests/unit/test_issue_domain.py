from backend.domain.issues import REGISTRY, IssueType, fingerprint, spec_for


def test_fingerprint_is_stable_and_path_normalised():
    a = fingerprint("missing_file", "movie", 1, r"C:\Movies\Film (2020)\Film.mkv")
    assert a == fingerprint("missing_file", "movie", 1, "c:/movies/film (2020)/FILM.MKV")
    assert a == fingerprint(IssueType.MISSING_FILE, "movie", 1, "C:/Movies/Film (2020)//Film.mkv")
    assert len(a) == 64


def test_fingerprint_separates_identity_fields():
    base = fingerprint("duplicate_file", "tv", 5, "/tv/Show/a.mkv", "S01E02")
    assert base != fingerprint("duplicate_file", "tv", 5, "/tv/Show/a.mkv", "S01E03")
    assert base != fingerprint("duplicate_file", "tv", 6, "/tv/Show/a.mkv", "S01E02")
    assert base != fingerprint("missing_file", "tv", 5, "/tv/Show/a.mkv", "S01E02")
    assert base == fingerprint("duplicate_file", "tv", 5, "/tv/Show/a.mkv", "s01e02")
    assert fingerprint("x", None, None, None) == fingerprint("x", None, None, "")


def test_registry_complete():
    assert set(REGISTRY) == set(IssueType)
    for t, spec in REGISTRY.items():
        assert spec.label and spec.type == t
        assert "dismiss" in spec.allowed_actions() and "reopen" in spec.allowed_actions()
    assert spec_for("nope") is None
    assert spec_for("missing_file").severity.value == "error"
