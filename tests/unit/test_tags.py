import pytest

from backend.domain.tags import requester_name


@pytest.mark.parametrize(("label", "name"), [
    ("12-alice", "alice"),
    ("12 - Alice Smith", "Alice Smith"),  # Seerr's older label format
    ("7-bob_99", "bob_99"),
    ("anime", None),
    ("4k-remux", None),
    ("12-", None),
    ("", None),
])
def test_requester_name(label, name):
    assert requester_name(label) == name
