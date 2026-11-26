"""Launch or reuse the configured direct native MiniMax H3 ComfyUI runtime."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import sys
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from videofactory_native import NoRedirect, config_at, connection_refused, local_url, start_comfy


HTTP = build_opener(ProxyHandler({}), NoRedirect())


@contextmanager
def start_lock(root: Path):
    import msvcrt

    root.mkdir(parents=True, exist_ok=True)
    with (root / "stack-start.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError("Another launcher is starting this model runtime. Wait for it to finish.") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def existing(origin: str, route: str):
    try:
        with HTTP.open(origin + route, timeout=4) as response:
            return json.load(response)
    except URLError as error:
        if connection_refused(error):
            return None
        raise ValueError(f"Cannot verify the process at {origin}. Startup did not replace it: {error}") from error


def validate_comfy(stats: object, config: dict) -> None:
    if not isinstance(stats, dict):
        raise ValueError("ComfyUI did not return a system status document")
    devices = stats.get("devices", [])
    argv = stats.get("system", {}).get("argv", [])
    pairs = (("--input-directory", "input_root"), ("--output-directory", "output_root"))
    paths_match = all(
        flag in argv and argv.index(flag) + 1 < len(argv)
        and Path(argv[argv.index(flag) + 1]).resolve() == Path(config[key]).resolve()
        for flag, key in pairs
    )
    if not isinstance(devices, list) or not any(device.get("type") == "cuda" for device in devices if isinstance(device, dict)):
        raise ValueError("ComfyUI did not report a CUDA device")
    if not paths_match:
        raise ValueError("ComfyUI input/output paths do not match this native H3 runtime configuration")


def start(config_path: Path) -> None:
    config = config_at(config_path)
    root = Path(config["runtime_root"])
    origin = local_url(config["comfy_origin"])
    with start_lock(root):
        stats = existing(origin, "/system_stats")
        if stats is None:
            print(f"Starting native MiniMax H3 ComfyUI at {origin}...", flush=True)
            start_comfy(config)
            stats = existing(origin, "/system_stats")
        validate_comfy(stats, config)
        print(f"Native MiniMax H3 ComfyUI ready at {origin}.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run this launcher on the Windows NVIDIA model host.")
    try:
        start(args.config.resolve(strict=True))
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        print(f"Native H3 startup incomplete: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
