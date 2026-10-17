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
