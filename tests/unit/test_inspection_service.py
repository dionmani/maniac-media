import pytest
import asyncio
from pathlib import Path
from backend.services.inspection_service import probe, ffprobe_path

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
    assert "-i" in args_called
    input_arg = args_called[args_called.index("-i") + 1]
    
    # Path might be different on Windows (C:\...) but the prefix should be file:
    assert input_arg.startswith("file:")
