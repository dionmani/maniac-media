import pytest
import asyncio
from pathlib import Path
from backend.services.inspection_service import probe, ffprobe_path, ffprobe_version

@pytest.mark.anyio
async def test_probe_command_args(monkeypatch):
    args_called = []
    
    async def mock_exec(*args, **kwargs):
        args_called.extend(args)
        
        class MockProc:
            returncode = 0
            async def communicate(self):
                return b'{"format": {"format_name": "matroska"}}', b''
            def kill(self): pass
            
        return MockProc()
        
    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr("backend.services.inspection_service.ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr("backend.services.inspection_service.long_path", lambda p: str(p))
    
    await probe(Path("/media/movies/movie.mkv"))
    
    assert "/usr/bin/ffprobe" in args_called
    assert "-protocol_whitelist" in args_called
    assert args_called[args_called.index("-protocol_whitelist") + 1] == "file"
    assert "-show_format" in args_called
    assert "-show_streams" in args_called
    assert "-print_format" in args_called
    assert args_called[args_called.index("-print_format") + 1] == "json"
    assert "-i" in args_called
    input_arg = args_called[args_called.index("-i") + 1]
    
    assert input_arg.startswith("file:")

@pytest.mark.anyio
async def test_probe_timeout(monkeypatch):
    async def mock_exec(*args, **kwargs):
        class HangingProc:
            returncode = None
            async def communicate(self):
                await asyncio.sleep(10)
                return b'', b''
            def kill(self):
                self.returncode = -9
            async def wait(self):
                return self.returncode
        return HangingProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", mock_exec)
    monkeypatch.setattr("backend.services.inspection_service.ffprobe_path", lambda: "/usr/bin/ffprobe")
    monkeypatch.setattr("backend.services.inspection_service.long_path", lambda p: str(p))

    with pytest.raises(RuntimeError, match="timeout"):
        await probe(Path("/media/movies/slow.mkv"), timeout=0.05)

def test_ffprobe_version_parsing(monkeypatch):
    import subprocess
    from backend.services import inspection_service
    inspection_service._cached_version = None

    class MockResult:
        stdout = "ffprobe version 7.0.2-static https://johnvansickle.com/ffmpeg/ Copyright (c) 2007-2024\nbuilt with gcc"

    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: MockResult())
    monkeypatch.setattr("backend.services.inspection_service.ffprobe_path", lambda: "/usr/bin/ffprobe")

    ver = ffprobe_version()
    assert ver == "7.0.2-static"


def test_every_ffprobe_invocation_is_locked_to_local_files(tmp_path):
    from backend.services.inspection_service import _decode_argv, _probe_argv

    crafted = tmp_path / "concat:evil.mkv"
    for argv in (_probe_argv("ffprobe", crafted), _decode_argv("ffprobe", crafted, "0%+10")):
        i = argv.index("-protocol_whitelist")
        assert argv[i + 1] == "file" and argv[i + 2] == "-i" and argv[i + 3].startswith("file:")
    assert "-count_frames" in _decode_argv("ffprobe", crafted, "0%+10")


def test_expected_runtime_for_multi_episode_files():
    from backend.services.inspection_service import expected_runtime_minutes

    assert expected_runtime_minutes([30, 30], None) == 60       # S01E01-E02 runs for both episodes
    assert expected_runtime_minutes([None, 25], 22) == 47       # per-episode gap filled from the series
    assert expected_runtime_minutes([None], None) is None
    assert expected_runtime_minutes([], 40) is None


def test_pre_import_check_rejects_only_on_positive_evidence(monkeypatch):
    from pathlib import Path

    from backend.core.settings import settings_manager
    from backend.services import inspection_service as svc

    monkeypatch.setattr(svc, "ffprobe_path", lambda: "/usr/bin/ffprobe")
    settings_manager.update(inspection_enabled=True, inspect_before_import=True, inspection_decode_test=False)
    try:
        no_video = {"format": {"format_name": "matroska", "duration": "60"}, "streams": [{"codec_type": "audio", "codec_name": "aac", "channels": 2}]}
        monkeypatch.setattr(svc, "probe_sync", lambda p, timeout=30: no_video)
        assert svc.pre_import_check(Path("x.mkv")).rejection == "No video stream found"

        def boom(p, timeout=30):
            raise RuntimeError("ffprobe failed: something unrelated")

        monkeypatch.setattr(svc, "probe_sync", boom)
        assert svc.pre_import_check(Path("x.mkv")) is None  # can't tell -> import proceeds unchecked

        settings_manager.update(inspect_before_import=False)
        assert svc.pre_import_check(Path("x.mkv")) is None
    finally:
        settings_manager.update(inspect_before_import=False)
