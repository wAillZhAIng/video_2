"""Reconcile saved Context-IR submissions before reopening the native proxy."""
from datetime import datetime
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit
from uuid import UUID

from videofactory_native import load, queue_idle, write


def prompt_identifier(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except ValueError:
        return False


def terminal_matches(entry, prompt_id, graph, client_id):
    if not isinstance(entry, dict):
        return False
    prompt = entry.get('prompt')
    status = entry.get('status')
    return (isinstance(prompt, list) and len(prompt) >= 4 and prompt[1] == prompt_id
            and prompt[2] == graph and isinstance(prompt[3], dict) and prompt[3].get('client_id') == client_id
            and isinstance(status, dict) and (status.get('completed') is True
                 or status.get('status_str') in {'error', 'failed', 'cancelled', 'interrupted'}))


def saved_output_state(output_root, graph, client_id):
    """Only the project's single per-job SaveVideo path proves absence of output."""
    saves = [node.get('inputs', {}).get('filename_prefix') for node in graph.values()
             if isinstance(node, dict) and node.get('class_type') == 'SaveVideo']
    if len(saves) != 1 or not isinstance(saves[0], str):
        return 'output_unverified'
    prefix = PurePosixPath(saves[0])
    if (len(prefix.parts) != 4 or prefix.parts[0] != 'projects' or prefix.name != client_id
            or not all(re.fullmatch(r'[A-Za-z0-9_-]{1,64}', part) for part in prefix.parts)):
        return 'output_unverified'
    root = Path(output_root).resolve(strict=True)
    folder = root.joinpath(*prefix.parts[:-1])
    if folder.resolve() != folder:
        return 'output_unverified'
    try:
        if any(item.name.startswith(client_id) for item in folder.iterdir()):
            return 'output_preserved'
    except FileNotFoundError:
        pass
    return 'interrupted_no_output'


def owned_worker_started_at(config):
    """Use only a runtime owner record that still matches the loopback listener."""
    import psutil

    try:
        owner = load(Path(config['runtime_root']) / 'comfy-owned-process.json')
        if owner.get('config_sha256') != hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest():
            return None
        process = psutil.Process(owner['pid'])
        started_at = process.create_time()
        if (started_at != owner['created'] or process.exe() != owner['executable']
                or process.cmdline() != owner['args']
                or Path(process.cwd()).resolve() != Path(config['comfy_root']).resolve()):
            return None
        port = urlsplit(config['comfy_origin']).port
        listeners = [c for c in process.net_connections(kind='inet') if c.status == 'LISTEN'
                     and c.laddr.port == port]
        if len(listeners) == 1 and listeners[0].laddr.ip == '127.0.0.1':
            return started_at
    except (OSError, ValueError, KeyError, TypeError, psutil.Error):
        pass
    return None


def reconcile_previous_submissions(runtime, *, comfy_started_at=None):
    """Use exact history first. A newer owned worker can prove old work stopped."""
    if not queue_idle(runtime.comfy('/queue')):
        raise ValueError('Comfy is busy; native supervisor will not adopt or re-submit existing work')
    archived = {}
    interrupted = {}
    for case in (Path(runtime.config['runtime_root']) / 'jobs').glob('job_*'):
        intent_file = case / 'submission-intent.json'
        if not intent_file.is_file():
            continue
        result_file = case / 'supervisor-result.json'
        terminal_file = case / 'terminal-history.json'
        result = load(result_file) if result_file.exists() else {}
        if result.get('terminal_verified') is True:
            if terminal_file.is_file() and prompt_identifier(result.get('prompt_id')):
                archived[result['prompt_id']] = case
            continue

        submitted = load(case / 'submitted_prompt.json')
        graph = submitted.get('prompt')
        response_file = case / 'submission_response.json'
        response = load(response_file) if response_file.is_file() else {}
        prompt_id = response.get('prompt_id') if isinstance(response, dict) else None
        if (not re.fullmatch(r'job_[a-f0-9]{32}', case.name) or submitted.get('client_id') != case.name
                or not isinstance(graph, dict) or graph.get('4', {}).get('class_type') != 'VideoFactoryLocalH3IRCompile'):
            raise ValueError('Earlier native job does not match its recorded submission')

        if prompt_identifier(prompt_id):
            history = load(terminal_file) if terminal_file.is_file() else None
            if history is None:
                history = runtime.comfy('/history/' + prompt_id).get(prompt_id)
            if history is not None:
                if not terminal_matches(history, prompt_id, graph, case.name):
                    raise ValueError('Earlier native job history does not match its recorded submission')
                write(terminal_file, history)
                write(result_file, {**result, 'prompt_id': prompt_id, 'terminal_verified': True,
                                    'recovered_from': 'original_terminal_history'})
                archived[prompt_id] = case
                continue

        stamp = datetime.fromisoformat(load(intent_file)['at'])
        if stamp.tzinfo is None:
            raise ValueError('Earlier native job has no reliable submission timestamp')
        last_submission = max(stamp.timestamp(), intent_file.stat().st_mtime)
        if response_file.is_file():
            last_submission = max(last_submission, response_file.stat().st_mtime)
        if (comfy_started_at is None or comfy_started_at <= last_submission
                or not queue_idle(runtime.comfy('/queue'))):
            raise ValueError('Earlier native submission is unresolved; its original queue or history must be verified')

        # This confirms the worker is gone, not that an unacknowledged job failed.
        outcome = (saved_output_state(runtime.config['output_root'], graph, case.name)
                   if prompt_identifier(prompt_id) else 'submission_unverified')
        receipt = {'protocol': 'native-context-recovery-v1', 'outcome': outcome,
                   'job_id': case.name, 'prompt_id': prompt_id,
                   'graph_sha256': hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(),
                   'submitted_at': last_submission, 'comfy_started_at': comfy_started_at}
        write(case / 'restart-recovery.json', receipt)
        if prompt_identifier(prompt_id):
            interrupted[prompt_id] = receipt
    return archived, interrupted


def archived_history(case, prompt_id):
    submitted = load(case / 'submitted_prompt.json')
    history = load(case / 'terminal-history.json')
    if (submitted.get('client_id') == case.name
            and terminal_matches(history, prompt_id, submitted.get('prompt'), case.name)):
        return history
    raise ValueError('Saved terminal history does not match its recorded submission')
