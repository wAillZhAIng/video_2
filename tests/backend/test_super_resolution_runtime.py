from pathlib import Path

from app.services import super_resolution


def test_runtime_uses_main_model_environment_when_available(monkeypatch, settings):
    monkeypatch.delenv('VIDEO_FACTORY_UPSCALE_PYTHON', raising=False)
    model_python = Path('/venv/main/bin/python')
    if model_python.is_file():
        assert super_resolution.runtime(settings)['python'] == str(model_python)


def test_runtime_honors_explicit_python_override(monkeypatch, settings):
    monkeypatch.setenv('VIDEO_FACTORY_UPSCALE_PYTHON', '/custom/python')
    assert super_resolution.runtime(settings)['python'] == '/custom/python'
