"""Configure and start the direct native MiniMax H3 ComfyUI runtime.

This helper deliberately owns only the local ComfyUI process. It does not
start a semantic compiler, a proxy, or a separate language model. The
application submits its frozen script, image references, and optional video
frames directly to the installed native H3 nodes.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


HERE = Path(__file__).resolve().parent
RELEASE = HERE.parents[1]
PROFILE = "native-h3-ref2va-v1"
HTTP = build_opener(ProxyHandler({}))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[override]
        return None


def load(path: Path):
    with Path(path).open("r", encoding="utf-8-sig") as stream:
        return json.load(stream)


def write(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def connection_refused(error: BaseException) -> bool:
    cause: BaseException | None = error
    while cause is not None:
        if isinstance(cause, ConnectionRefusedError):
            return True
        if isinstance(cause, URLError) and isinstance(cause.reason, BaseException):
            cause = cause.reason
            continue
        cause = cause.__cause__ or cause.__context__
    return False


def local_url(value: str) -> str:
    parsed = urlsplit(str(value).strip())
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("ComfyUI address must be an HTTP loopback origin")
    if parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError("ComfyUI address must be an origin without credentials, path, query, or fragment")
    if not parsed.port:
        raise ValueError("ComfyUI address must include its listening port")
    host = "[::1]" if parsed.hostname == "::1" else parsed.hostname
    return f"http://{host}:{parsed.port}"


def request_json(origin: str, route: str, *, timeout: float = 8, method: str = "GET", body: bytes | None = None):
    request = Request(local_url(origin) + route, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with HTTP.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def queue_idle(value) -> bool:
    return isinstance(value, dict) and not value.get("queue_running") and not value.get("queue_pending")


def config_at(path: Path) -> dict:
    config = load(Path(path))
    if not isinstance(config, dict) or config.get("profile_id") != PROFILE:
        raise ValueError("This is not a native H3 Ref2VA runtime configuration")
    for key in ("runtime_root", "comfy_root", "comfy_python", "comfy_origin", "input_root", "output_root"):
        if not config.get(key):
            raise ValueError(f"Runtime configuration is missing {key}")
    config["comfy_origin"] = local_url(str(config["comfy_origin"]))
    return config


def _runtime_paths(config: dict) -> list[Path]:
    return [Path(config[key]).expanduser().resolve() for key in ("runtime_root", "input_root", "output_root")]


def initialize(args: argparse.Namespace) -> int:
    runtime = args.runtime_root.expanduser().resolve()
    comfy = args.comfy_root.expanduser().resolve()
    python = args.comfy_python.expanduser().resolve()
    origin = local_url(args.comfy_origin)
    if not (comfy / "main.py").is_file():
        raise ValueError(f"ComfyUI root does not contain main.py: {comfy}")
    if not python.is_file():
        raise ValueError(f"ComfyUI Python does not exist: {python}")
    runtime.mkdir(parents=True, exist_ok=True)
    config = {
        "schema_version": 2,
        "profile_id": PROFILE,
        "runtime_root": str(runtime),
        "comfy_root": str(comfy),
        "comfy_python": str(python),
        "comfy_origin": origin,
        "input_root": str(runtime / "input"),
        "output_root": str(runtime / "output"),
        "model_root": str(args.model_root.expanduser().resolve()) if args.model_root else None,
        "ffmpeg_path": str(args.ffmpeg_path.expanduser().resolve()) if args.ffmpeg_path else None,
        "ffprobe_path": str(args.ffprobe_path.expanduser().resolve()) if args.ffprobe_path else None,
    }
    for directory in _runtime_paths(config) + [runtime / "logs", runtime / "user", runtime / "tmp"]:
        directory.mkdir(parents=True, exist_ok=True)
    target = runtime / "native-runtime.json"
    write(target, config)
    print(json.dumps({"config": str(target), "profile_id": PROFILE, "weights_downloaded": False}, ensure_ascii=False))
    return 0


def _tool_check(config: dict, name: str) -> dict:
    configured = config.get(f"{name}_path")
    executable = str(configured) if configured else shutil.which(name)
    record = {"kind": name, "ok": False, "path": executable}
    if not executable:
        record["detail"] = f"{name} was not found; configure {name}_path or PATH"
        return record
    try:
        completed = subprocess.run([executable, "-version"], capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as error:
        record["detail"] = str(error)
        return record
    first = (completed.stdout or completed.stderr).splitlines()
    record.update(ok=completed.returncode == 0 and bool(first), version=first[0] if first else None)
    return record


def check(config: dict, hash_models: bool = False) -> dict:
    del hash_models  # Actual node/model compatibility is checked through ComfyUI /object_info before dispatch.
    comfy_root = Path(config["comfy_root"])
    python = Path(config["comfy_python"])
    checks = [
        {"kind": "comfy_main", "ok": (comfy_root / "main.py").is_file(), "path": str(comfy_root / "main.py")},
        {"kind": "comfy_python", "ok": python.is_file(), "path": str(python)},
    ]
    for directory in _runtime_paths(config):
        checks.append({"kind": "directory", "ok": directory.is_dir(), "path": str(directory)})
    if config.get("model_root"):
        model_root = Path(str(config["model_root"]))
        checks.append({"kind": "model_root", "ok": model_root.is_dir(), "path": str(model_root)})
    checks.extend(_tool_check(config, name) for name in ("ffmpeg", "ffprobe"))
    result = {
        "schema_version": 2,
        "profile_id": PROFILE,
        "ready": all(bool(item.get("ok")) for item in checks if item["kind"] not in {"ffmpeg", "ffprobe"}),
        "checks": checks,
        "weights_downloaded": False,
        "generation_tested": False,
        "note": "Node and model compatibility is verified by the application against ComfyUI /object_info before dispatch.",
    }
    write(Path(config["runtime_root"]) / "installation-check.json", result)
    return result


def environment(config: dict) -> dict[str, str]:
    env = dict(os.environ)
    runtime = Path(config["runtime_root"])
    env.update({
        "VF_RUNTIME_ROOT": str(runtime),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "HF_HOME": str(runtime / "cache" / "huggingface"),
        "TORCH_HOME": str(runtime / "cache" / "torch"),
    })
    tool_dirs = [str(Path(str(config[f"{name}_path"])).resolve().parent) for name in ("ffmpeg", "ffprobe") if config.get(f"{name}_path")]
    if tool_dirs:
        env["PATH"] = os.pathsep.join(dict.fromkeys(tool_dirs)) + os.pathsep + env.get("PATH", "")
    return env


def start_comfy(config: dict) -> subprocess.Popen | None:
    readiness = check(config)
    if not readiness["ready"]:
        raise ValueError("Native H3 runtime prerequisites failed; see installation-check.json")
    origin = local_url(config["comfy_origin"])
    try:
        request_json(origin, "/system_stats", timeout=3)
    except URLError as error:
        if not connection_refused(error):
            raise ValueError(f"Cannot determine ComfyUI availability: {error}") from error
    except HTTPError as error:
        raise ValueError(f"ComfyUI listener returned HTTP {error.code}; no duplicate process was started") from error
    else:
        return None
    runtime = Path(config["runtime_root"])
    comfy_root = Path(config["comfy_root"])
    port = urlsplit(origin).port
    assert port is not None
    (runtime / "user").mkdir(parents=True, exist_ok=True)
    (runtime / "logs").mkdir(parents=True, exist_ok=True)
    args = [str(config["comfy_python"]), "-X", "utf8", "-B", str(comfy_root / "main.py"),
            "--listen", "127.0.0.1", "--port", str(port), "--disable-auto-launch",
            "--disable-async-offload", "--disable-pinned-memory", "--cache-none",
            "--input-directory", str(config["input_root"]), "--output-directory", str(config["output_root"]),
            "--user-directory", str(runtime / "user")]
    extra_paths = runtime / "extra-model-paths.yaml"
    if extra_paths.is_file():
        args.extend(["--extra-model-paths-config", str(extra_paths)])
    stdout = (runtime / "logs" / "comfy.stdout.log").open("ab")
    stderr = (runtime / "logs" / "comfy.stderr.log").open("ab")
    process = subprocess.Popen(args, cwd=comfy_root, env=environment(config), stdout=stdout, stderr=stderr,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    write(runtime / "comfy-owned-process.json", {"pid": process.pid, "args": args, "profile_id": PROFILE})
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise ValueError("ComfyUI exited during startup; inspect logs/comfy.stderr.log")
        try:
            stats = request_json(origin, "/system_stats", timeout=2)
        except (TimeoutError, URLError):
            time.sleep(0.5)
            continue
        if isinstance(stats, dict) and isinstance(stats.get("devices"), list):
            print(json.dumps({"started_pid": process.pid, "readiness": "ready", "origin": origin}, ensure_ascii=False))
            return process
        time.sleep(0.5)
    raise ValueError("ComfyUI did not become ready; inspect logs/comfy.stderr.log")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init")
    init.add_argument("--comfy-root", type=Path, required=True)
    init.add_argument("--runtime-root", type=Path, required=True)
    init.add_argument("--comfy-python", type=Path, required=True)
    init.add_argument("--model-root", type=Path)
    init.add_argument("--comfy-origin", default="http://127.0.0.1:8188")
    init.add_argument("--ffmpeg-path", type=Path)
    init.add_argument("--ffprobe-path", type=Path)
    for name in ("check", "start-comfy"):
        child = commands.add_parser(name)
        child.add_argument("--config", type=Path, required=True)
        if name == "check":
            child.add_argument("--hash-models", action="store_true")
    args = parser.parse_args()
    if args.command == "init":
        return initialize(args)
    config = config_at(args.config)
    if args.command == "check":
        result = check(config, args.hash_models)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 2
    start_comfy(config)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as error:
        print(json.dumps({"error": str(error), "weights_downloaded": False}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
