"""Manage only the native services belonging to this VideoFactory checkout."""

from __future__ import annotations

import contextlib
import json
import msvcrt
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import urllib.request

import psutil


ROOT = Path(__file__).resolve().parents[2]
INFERENCE = ROOT / ".local-runtime/inference"
MANAGER = ROOT / ".local-runtime/manager"
CONFIG = INFERENCE / "native-runtime.json"
NATIVE_SCRIPT = ROOT / "scripts/local_inference/videofactory_native.py"
COMFY_SCRIPT = ROOT / ".local-runtime/comfy/main.py"
AUTHOR_SCRIPT = ROOT / "scripts/serve_screenplay_qwen.py"
ASR_SCRIPT = ROOT / "scripts/serve_speech_recognition.py"
ASR_PYTHON = ROOT / ".local-runtime/asr-env/Scripts/python.exe"
ASR_MODEL = Path("E:/Models/faster-whisper-large-v3")
SEEDVR_RELAY = ROOT / "scripts/local_inference/share_comfy_with_seedvr.py"
VITE_SCRIPT = ROOT / "frontend/node_modules/vite/bin/vite.js"
JOB_SCRIPTS = tuple(
    ROOT / "scripts" / name for name in (
        "run_cosyvoice3.py", "run_indextts25.py", "run_latentsync15.py", "upscale_video_realesrgan.py",
    )
)

PORTS = {
    "后端": 8765,
    "网页": 5173,
    "剧本 Qwen": 8771,
    "语音识别": 8778,
    "ComfyUI": 8188,
    "SeedVR2 共用端口": 8189,
}
STOP_ORDER = (
    ("后端",),
    ("剧本 Qwen", "语音识别", "SeedVR2 共用端口", "ComfyUI", "辅助制作进程", "网页"),
)
HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class ManagerError(RuntimeError):
    pass


def report(message, *, persist=True):
    print(message, flush=True)
    if not persist:
        return
    try:
        with (MANAGER / "controller.log").open("a", encoding="utf-8") as log:
            log.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
    except OSError:
        pass


def checked_workspace_path(path):
    """Reject junctions and symlinks before removing or writing workspace files."""
    if not path.is_relative_to(ROOT) or path == ROOT:
        raise ManagerError(f"路径不在当前项目中：{path}")
    cursor = path
    while cursor != ROOT:
        try:
            info = cursor.lstat()
            if info.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ManagerError(f"路径含有链接或挂载点，未处理：{cursor}")
        except FileNotFoundError:
            pass
        cursor = cursor.parent
    if not path.resolve().is_relative_to(ROOT):
        raise ManagerError(f"路径解析后离开当前项目：{path}")
    return path


@contextlib.contextmanager
def operation_lock():
    checked_workspace_path(MANAGER / "controller.log")
    checked_workspace_path(MANAGER / "operation.lock")
    MANAGER.mkdir(parents=True, exist_ok=True)
    with (MANAGER / "operation.lock").open("a+b") as lock:
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise ManagerError("已有管理操作正在执行。请从同一个管理窗口操作。") from error
        try:
            yield
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def option(args, name):
    for index, arg in enumerate(args):
        if arg == name and index + 1 < len(args):
            return args[index + 1]
        if arg.startswith(name + "="):
            return arg.partition("=")[2]
    return ""


def same_path(value, expected, process):
    if not value or Path(value).name.casefold() != expected.name.casefold():
        return False
    path = Path(value)
    if not path.is_absolute():
        path = Path(process.cwd()) / path
    return path.resolve() == expected


def python_target(args):
    """Read the interpreter target, excluding arguments to the invoked script."""
    cursor = 0
    while cursor < len(args) and args[cursor].startswith("-"):
        flag = args[cursor]
        if flag in ("-m", "-c"):
            return flag, args[cursor + 1 : cursor + 2], args[cursor + 2 :]
        if flag == "--":
            cursor += 1
            break
        cursor += 2 if flag in ("-W", "-X") else 1
    return "script", args[cursor : cursor + 1], args[cursor + 1 :]


def role_of(process):
    try:
        with process.oneshot():
            name = process.name().casefold()
            if name not in ("python.exe", "pythonw.exe", "node.exe"):
                return None
            args = process.cmdline()[1:]
            if name == "node.exe":
                if args and same_path(args[0], VITE_SCRIPT, process):
                    return "网页"
                return None
            kind, target, options = python_target(args)
            if kind == "-m" and target == ["uvicorn"] and options[:1] == ["app.main:app"]:
                if same_path(option(options, "--app-dir"), ROOT / "backend", process):
                    return "后端"
            if kind != "script" or not target:
                return None
            script = target[0]
            if same_path(script, COMFY_SCRIPT, process):
                if (same_path(option(options, "--input-directory"), INFERENCE / "input", process)
                        and same_path(option(options, "--output-directory"), INFERENCE / "output", process)):
                    return "ComfyUI"
            if same_path(script, AUTHOR_SCRIPT, process):
                return "剧本 Qwen"
            if same_path(script, ASR_SCRIPT, process) and same_path(option(options, "--model-dir"), ASR_MODEL, process):
                return "语音识别"
            if same_path(script, SEEDVR_RELAY, process):
                return "SeedVR2 共用端口"
            if any(same_path(script, job_script, process) for job_script in JOB_SCRIPTS):
                return "辅助制作进程"
    except (psutil.Error, OSError, RuntimeError, ValueError):
        return None
    return None


def owned_processes():
    # Use fresh Process identities so a recycled PID cannot inherit an earlier match.
    psutil.process_iter.cache_clear()
    result = {role: [] for roles in STOP_ORDER for role in roles}
    for process in psutil.process_iter():
        role = role_of(process)
        if role:
            result[role].append(process)
    return result


def listeners():
    bound = {port: set() for port in PORTS.values()}
    for connection in psutil.net_connections(kind="tcp"):
        if connection.status == psutil.CONN_LISTEN and connection.laddr.port in bound:
            bound[connection.laddr.port].add(connection.pid)
    return bound


def port_is_owned(role):
    port = PORTS[role]
    pids = listeners()[port]
    for pid in pids:
        try:
            belongs = pid is not None and role_of(psutil.Process(pid)) == role
        except psutil.NoSuchProcess:
            continue
        if not belongs:
            raise ManagerError(f"{role}端口 {port} 被其他程序占用（PID {pid}）。请释放端口后重试。")
    return bool(pids)


def request(role, route):
    url = f"http://127.0.0.1:{PORTS[role]}{route}"
    try:
        with HTTP.open(url, timeout=2.5) as response:
            raw = response.read(2 * 1024**2)
        return raw if route == "/" else json.loads(raw)
    except (OSError, ValueError):
        return None


def response_fields(value):
    return value if isinstance(value, dict) else {}


def wait_ready(role, check, timeout, process=None):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if process is not None and process.poll() is not None:
            raise ManagerError(f"{role}启动后退出（退出码 {process.returncode}）。请检查启动日志。")
        if port_is_owned(role) and check():
            return
        time.sleep(0.5)
    raise ManagerError(f"{role}在 {timeout} 秒内未就绪。请检查启动日志，或用一键关闭回收卡住的进程。")


def service_log(role):
    # Port names give stable ASCII filenames even when inspected on another machine.
    log = checked_workspace_path(MANAGER / "logs" / f"{PORTS[role]}.log")
    log.parent.mkdir(parents=True, exist_ok=True)
    return log


def launch(role, args, cwd=ROOT, env=None):
    with service_log(role).open("ab") as log:
        return subprocess.Popen(
            [str(arg) for arg in args], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )


def ensure_service(role, check, timeout, spawn):
    has_listener = port_is_owned(role)
    instances = owned_processes()[role]
    if has_listener or instances:
        report(f"正在检查已有的{role}（{PORTS[role]}）…")
        process = None
    else:
        report(f"正在启动{role}（{PORTS[role]}）…")
        process = spawn()
    wait_ready(role, check, timeout, process)
    report(f"{role}已就绪（{PORTS[role]}）。")


def required_file(path, description):
    if not path.is_file():
        raise ManagerError(f"找不到{description}：{path}")
    return path


def start():
    # Check the real web path as well as the backend before opening a browser.
    def api_healthy(role):
        return response_fields(request(role, "/api/health")).get("status") == "ok"

    ensure_service(
        "后端", lambda: api_healthy("后端"), 55,
        lambda: launch("后端", [
            required_file(ROOT / ".venv/Scripts/python.exe", "项目 Python 环境"),
            "-X", "utf8", "-B", "-m", "uvicorn", "app.main:app",
            "--app-dir", ROOT / "backend", "--host", "127.0.0.1", "--port", PORTS["后端"],
        ]),
    )
    frontend_env = dict(os.environ, VITE_DEV_API_TARGET=f"http://127.0.0.1:{PORTS['后端']}")
    ensure_service(
        "网页", lambda: api_healthy("网页") and b'id="root"' in (request("网页", "/") or b""), 45,
        lambda: launch("网页", [
            required_file(Path(shutil.which("node") or "D:/Node/node.exe"), "Node.js"),
            required_file(VITE_SCRIPT, "Vite 依赖"), "--host", "127.0.0.1",
            "--port", PORTS["网页"], "--strictPort",
        ], cwd=ROOT / "frontend", env=frontend_env),
    )
    sys.path.insert(0, str(NATIVE_SCRIPT.parent))
    import videofactory_native as native

    required_file(CONFIG, "本机模型运行配置")
    config = native.config_at(CONFIG)
    expected_paths = {
        "runtime_root": INFERENCE,
        "comfy_root": COMFY_SCRIPT.parent,
        "input_root": INFERENCE / "input",
        "output_root": INFERENCE / "output",
    }
    if (any(Path(config[key]).resolve() != path for key, path in expected_paths.items())
            or config["comfy_origin"].rstrip("/") != f"http://127.0.0.1:{PORTS['ComfyUI']}"):
        raise ManagerError(f"本机模型路径或端口与本项目管理配置不一致：{CONFIG}")
    for name in ("input", "output", "jobs", "logs", "tmp", "user"):
        checked_workspace_path(INFERENCE / name).mkdir(parents=True, exist_ok=True)

    def author_healthy():
        health = response_fields(request("剧本 Qwen", "/health"))
        return (
            health.get("service") == "video-factory-screenplay-qwen-v1"
            and Path(health.get("workspace") or "").resolve() == ROOT
        )

    # The authoring model is optional and independent from direct native H3 video generation.
    author_python = Path("E:/VideoFactoryRuntime/envs/screenplay_qwen/Scripts/python.exe")
    if not author_python.is_file():
        author_python = ROOT / ".venv/Scripts/python.exe"
    ensure_service(
        "剧本 Qwen", author_healthy, 55,
        lambda: launch("剧本 Qwen", [
            required_file(author_python, "剧本模型 Python 环境"), "-X", "utf8", "-B", AUTHOR_SCRIPT,
        ]),
    )

    def comfy_healthy():
        return isinstance(response_fields(request("ComfyUI", "/system_stats")).get("devices"), list)

    ensure_service("ComfyUI", comfy_healthy, 90, lambda: native.start_comfy(config))
    report("网页、后端和原生 MiniMax H3 ComfyUI 已就绪。")
    print("VF_OPEN_BROWSER", flush=True)
    try:
        def shared_comfy_healthy():
            raw = response_fields(request("ComfyUI", "/system_stats")).get("system", {}).get("argv")
            relay = response_fields(request("SeedVR2 共用端口", "/system_stats")).get("system", {}).get("argv")
            return bool(raw and relay == raw)

        ensure_service("SeedVR2 共用端口", shared_comfy_healthy, 12,
                       lambda: launch("SeedVR2 共用端口", [sys.executable, "-X", "utf8", "-B", SEEDVR_RELAY]))
    except (OSError, ManagerError) as error:
        report(f"SeedVR2 共用端口未启动：{error}")
    asr_receipt = ASR_MODEL / "deployment-model.json"
    if ASR_PYTHON.is_file() and asr_receipt.is_file():
        try:
            model_id = json.loads(asr_receipt.read_text(encoding="utf-8-sig"))["model_id"]

            def asr_healthy():
                health = response_fields(request("语音识别", "/health"))
                return (health.get("service") == "video-factory-local-asr-v1"
                        and health.get("ready") is True and health.get("model_id") == model_id)

            ensure_service("语音识别", asr_healthy, 180, lambda: launch("语音识别", [
                ASR_PYTHON, "-X", "utf8", "-B", ASR_SCRIPT, "--model-dir", ASR_MODEL,
                "--port", PORTS["语音识别"], "--threads", 2,
            ]))
        except (KeyError, OSError, ValueError, ManagerError) as error:
            report(f"可选语音识别服务暂未就绪：{error}")


def force_stop(roots, role_names):
    processes = set(roots)
    for process in roots:
        try:
            processes.update(process.children(recursive=True))
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            report(f"无法读取 PID {process.pid} 的子进程，正在尝试关闭已识别的进程。")
    if not processes:
        return
    report(f"正在关闭 {'、'.join(role_names)} 的 {len(processes)} 个项目进程…")
    # Kill dispatchers first; their descendants cannot be re-created by those dispatchers.
    for process in [*roots, *(processes - set(roots))]:
        try:
            process.kill()
        except psutil.NoSuchProcess:
            pass
        except psutil.AccessDenied:
            report(f"Windows 拒绝关闭 PID {process.pid}。请确认管理器已通过 UAC 提权。")
    psutil.wait_procs(processes, timeout=2)


def stop():
    for _ in range(2):
        for roles in STOP_ORDER:
            current = owned_processes()
            force_stop([process for role in roles for process in current[role]], roles)
        remaining = {role: [p.pid for p in ps] for role, ps in owned_processes().items() if ps}
        if not remaining:
            report("本项目的网页、后端和模型进程已关闭。")
            return
    raise ManagerError(f"以下项目进程仍未退出：{remaining}")


def verify_local_storage():
    """Refuse a partial reset if this instance points at another data store."""
    data = ROOT / "data"
    sys.path.insert(0, str(ROOT / "backend"))
    from app.core.config import load_local_environment

    def verify(database_url, media_root):
        prefix = "sqlite:///"
        if not database_url.startswith(prefix):
            raise ManagerError("一键清理只支持本项目目录中的 SQLite 数据库。")
        paths = [checked_workspace_path(ROOT / value).resolve() for value in (database_url[len(prefix):], media_root)]
        for path in paths:
            if not path.is_relative_to(data):
                raise ManagerError("检测到项目 data 之外的存储配置，已取消清理。")
        return tuple(paths)

    load_local_environment()
    default_db = "sqlite:///./data/video_factory.db"
    default_media = "./data/media"
    database = os.getenv("DATABASE_URL", default_db)
    media = os.getenv("MEDIA_ROOT", default_media)
    configured = verify(database, media)
    for server in owned_processes()["后端"]:
        try:
            env = server.environ()
        except psutil.NoSuchProcess:
            continue
        except psutil.Error as error:
            raise ManagerError("无法核对正在运行的后端的数据库与素材目录，已取消清理。") from error
        active = verify(env.get("DATABASE_URL", database), env.get("MEDIA_ROOT") or media)
        if active != configured:
            raise ManagerError("当前后端使用的存储和启动配置不一致，已取消清理。")
    return configured


def reset_caches_and_data(database, media):
    # This allowlist is project-scoped. Inference code, configs, model checksums
    # and downloaded weights are dependencies of the next clean start.
    runtime_directories = [
        ROOT / "frontend/node_modules/.vite", ROOT / "frontend/node_modules/.vite-temp",
        ROOT / ".local-runtime/comfy/input", ROOT / ".local-runtime/comfy/output",
        ROOT / ".local-runtime/comfy/temp",
        *(INFERENCE / name for name in ("input", "output", "jobs", "logs", "tmp", "cache/native_h3")),
        MANAGER / "logs",
    ]
    transient_files = [
        *(INFERENCE / name for name in ("comfy-owned-process.json", "preflight.log")),
        MANAGER / "controller.log",
    ]
    for path in (*runtime_directories, *transient_files):
        checked_workspace_path(path)

    sys.path.insert(0, str(ROOT / "scripts"))
    from app.services.runtime_registry import FAMILIES, MANIFEST_ROOT
    from reset_business_data import reset
    retained_files = []
    for filename, _helper, _model in FAMILIES.values():
        manifest = MANIFEST_ROOT / filename
        try:
            smoke = json.loads(manifest.read_text(encoding="utf-8-sig")).get("smoke_test", {})
        except FileNotFoundError:
            continue
        retained_files.extend(smoke[key] for key in ("receipt_path", "output_path") if smoke.get(key))
    records = reset(ROOT, database, media, runtime_targets=runtime_directories, retained_files=retained_files)
    for path in transient_files:
        path.unlink(missing_ok=True)
    report(f"已清理 {records} 条业务记录、相关文件和运行缓存。模型配置与密钥已保留。", persist=False)


def clean():
    # Check for an unrelated instance before stopping any of this project's services.
    for role in PORTS:
        port_is_owned(role)
    storage = verify_local_storage()
    stop()
    bound = listeners()
    if any(bound[port] for port in PORTS.values()):
        busy_ports = ", ".join(str(port) for port, pids in bound.items() if pids)
        raise ManagerError(f"端口 {busy_ports} 上还有未识别的服务，已停止清理，请先检查端口。")
    reset_caches_and_data(*storage)


def main():
    actions = {"start": start, "stop": stop, "clean": clean}
    if len(sys.argv) != 2 or sys.argv[1] not in actions:
        return 2
    try:
        with operation_lock():
            try:
                actions[sys.argv[1]]()
            except (ManagerError, OSError, RuntimeError, ValueError, ImportError, sqlite3.Error) as error:
                report(f"操作未完成。{error}")
                return 1
    except (ManagerError, OSError) as error:
        print(f"操作未完成。{error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
