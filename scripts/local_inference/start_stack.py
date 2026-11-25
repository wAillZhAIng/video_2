"""Launch or reuse the Windows host's configured ComfyUI and Context-IR runtime."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ipaddress
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from videofactory_native import NoRedirect, PROFILE, config_at, connection_refused, local_url, start_comfy


HTTP = build_opener(ProxyHandler({}), NoRedirect())


@contextmanager
def start_lock(root):
    import msvcrt

    with (root / 'stack-start.lock').open('a+b') as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ValueError('Another launcher is starting this model runtime. Wait for it to finish.') from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def existing(origin, route):
    try:
        with HTTP.open(origin + route, timeout=4) as response:
            return json.load(response)
    except URLError as error:
        if connection_refused(error):
            return None
        raise ValueError(f'Cannot verify the process at {origin}. Startup did not replace it: {error}') from error


def validate_comfy(stats, config):
    argv = stats.get('system', {}).get('argv', []) if isinstance(stats, dict) else []
    devices = stats.get('devices', []) if isinstance(stats, dict) else []
    flags = ('--disable-async-offload', '--disable-pinned-memory', '--disable-comfy-compiler', '--cache-none')
    pairs = (('--input-directory', 'input_root'), ('--output-directory', 'output_root'))
    if (not any(device.get('type') == 'cuda' for device in devices) or not all(flag in argv for flag in flags)
            or not all(flag in argv and argv.index(flag) + 1 < len(argv)
                       and Path(argv[argv.index(flag) + 1]).resolve() == Path(config[key]).resolve()
                       for flag, key in pairs)):
        raise ValueError(f"The ComfyUI at {config['comfy_origin']} does not match this runtime's paths and memory settings.")


def runtime_state(health, comfy_origin):
    if not isinstance(health, dict):
        raise ValueError('Unexpected response from the configured Context-IR port.')
    comfy = health.get('comfyui') or {}
    if (health.get('schema_version') != 1 or health.get('service') != 'video-factory-native-context-runtime-v1'
            or health.get('profile_id') != PROFILE or str(comfy.get('origin', '')).rstrip('/') != comfy_origin):
        raise ValueError('The configured Context-IR port is occupied by a different runtime.')
    if health.get('ready') is True:
        return 'ready'
    if (health.get('busy') is True and health.get('watcher_running') is True
            and (health.get('source_baseline') or {}).get('ok') is True
            and (health.get('active_prompts') or comfy.get('queue_running'))):
        return 'busy with a real job'
    return None


def start(config_path):
    config = config_at(config_path)
    root = Path(config['runtime_root'])
    root.mkdir(parents=True, exist_ok=True)
    raw = local_url(config['comfy_origin'])
    listen = ipaddress.ip_address(config.get('listen', '127.0.0.1'))
    listen = '127.0.0.1' if listen.is_unspecified else str(listen)
    authority = f'[{listen}]' if ':' in listen else listen
    context = f"http://{authority}:{int(config.get('port', 8788))}"
    # Only the configured local listener is contacted. For a private-IP binding,
    # connect directly with no environment proxy and no redirect handling.
    from native_supervisor import validate_listener

    validate_listener(config)
    with start_lock(root):
        if existing(raw, '/system_stats') is None:
            print(f'Starting ComfyUI at {raw}...', flush=True)
            start_comfy(config)
        validate_comfy(existing(raw, '/system_stats'), config)
        print(f'ComfyUI ready at {raw}.', flush=True)

        health_route = '/video_factory/runtime'
        health = existing(context, health_route)
        process = None
        log = root / 'logs/context-ir-launcher.log'
        if health is None:
            log.parent.mkdir(parents=True, exist_ok=True)
            command = [config['comfy_python'], '-X', 'utf8', '-B', '-u',
                       str(Path(__file__).with_name('videofactory_native.py')), 'serve', '--config', str(config_path)]
            print(f'Starting Context-IR at {context}...', flush=True)
            with log.open('ab') as stream:
                process = subprocess.Popen(command, cwd=root, stdin=subprocess.DEVNULL, stdout=stream,
                                           stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)

        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            if health is not None and (state := runtime_state(health, raw)):
                print(f'Context-IR {state} at {context}.', flush=True)
                return
            if process is not None and process.poll() is not None:
                raise ValueError(f'Context-IR exited ({process.returncode}). See {log}.')
            time.sleep(0.75)
            health = existing(context, health_route)
        raise ValueError(f'Context-IR did not become ready. Check {context}{health_route} and {log}.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('Run this launcher on the Windows NVIDIA model host.')
    try:
        start(args.config.resolve(strict=True))
    except (KeyError, OSError, ValueError, RuntimeError) as error:
        print(f'Native model startup incomplete: {error}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
