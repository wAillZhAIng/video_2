from __future__ import annotations
import hashlib, importlib, importlib.util, json, os, sys
from pathlib import Path

ROOT = Path(r"D:\人物一致性")
JOB = ROOT / "logs/jobs/job_575284aa3dc44769ae6438f725f70edc"
OUTPUT = ROOT / ".local-runtime/inference/output/h3_local_backend_profile_v3"
for key, value in {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "H3IR_GUIDED_DECODING": "1",
    "H3IR_LLM_THINKING": "0",
    "H3IR_LLM_MAX_TOKENS": "4096",
}.items():
    os.environ[key] = value
sys.path.insert(0, str(ROOT / "backend"))
from app.services.h3_context_provider import prepare_context_compilation


def load_package(name: str, directory: Path):
    spec = importlib.util.spec_from_file_location(
        name, directory / "__init__.py", submodule_search_locations=[str(directory)]
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load package {name} from {directory}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def input_scalar(value):
    # ComfyUI stores linked socket inputs as [node_id, output_index].
    if isinstance(value, list) and len(value) == 1:
        return value[0]
    return value


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()

raw = json.loads((JOB / "submitted_prompt.json").read_text(encoding="utf-8"))
nodes = raw["prompt"]
inputs = nodes["4"]["inputs"]
setup_inputs = nodes["1"]["inputs"]
source = str(inputs["intent"])
marker = "\nWrite the actual camera action inside the numbered shot as prose."
if marker not in source:
    raise RuntimeError("Expected prior app-generated camera menu suffix is absent; refusing an ambiguous replay")
source = source.rsplit(marker, 1)[0]
seconds = float(inputs["seconds"])
seed = int(inputs["seed"])
prepared = prepare_context_compilation(
    source, "", [], [], {"duration_seconds": seconds}, silent=bool(inputs["silent"])
)
tray, declarations, normalized_intent, production_contract = prepared
if tray:
    raise RuntimeError("This saved task was expected to have an empty media tray")

node_dir = ROOT / ".local-runtime/comfy/custom_nodes/ComfyUI-OpenH3-IR"
package = load_package("vf_context_ir_replay", node_dir)
client = importlib.import_module("vf_context_ir_replay.h3ir_client")
compiler = importlib.import_module("vf_context_ir_replay.compiler")
guard_dir = ROOT / ".local-runtime/comfy/custom_nodes/VideoFactoryLocalVLMGuard"
guard_package = load_package("vf_context_ir_guard", guard_dir)
guard = importlib.import_module("vf_context_ir_guard.local_h3ir_backend_profile")

url = str(setup_inputs["llm_url"])
model = str(setup_inputs["llm_model"])
setup = {"server": str(setup_inputs.get("server", "")), "llm_url": url, "llm_model": model}
payload = client.build_payload(
    normalized_intent,
    seconds=seconds,
    aspect=str(inputs["aspect"]),
    creativity=str(inputs["creativity"]),
    effort=str(inputs["effort"]),
    megapixels=float(inputs["megapixels"]),
    seed=seed,
    silent=bool(inputs["silent"]),
    shots=input_scalar(inputs["shots"]),
    assets=[],
    transcripts={},
    spoken=(),
    spoken_language=str(inputs["spoken_language"]),
)
report_root = OUTPUT
report_root.mkdir(parents=True, exist_ok=True)
receipt_file = None
result = None
failure = None
try:
    with guard.profile_scope(
        report_root, setup, seed, compiler_function=compiler.compile_here
    ) as receipt_file:
        scope_dir = Path(receipt_file).parent
        guard.write_json(scope_dir / "compile-replay-input-metadata.json", {
            "replay_job_id": JOB.name,
            "source_prompt_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "compiled_intent_sha256": hashlib.sha256(normalized_intent.encode("utf-8")).hexdigest(),
            "node_payload_sha256": guard.digest(payload),
            "seed": seed,
            "seconds": seconds,
            "aspect": inputs["aspect"],
            "megapixels": inputs["megapixels"],
            "shot_count": 1,
            "media_asset_count": 0,
            "production_contract": production_contract,
            "normalization_report": production_contract.get("compiler_input_normalization"),
            "execution": "actual OpenH3-IR compile_here under project source=written gate; H3 load/sampling not invoked",
        })
        result = compiler.compile_here(
            payload,
            sha_of=sha256_file,
            llm_url=url,
            llm_model=model,
            timeout=float(setup_inputs.get("timeout_s", 900)),
        )
        guard.write_json(scope_dir / "compile-only-result.json", {
            "status": "COMPILE_RETURNED",
            "result_keys": sorted(result) if isinstance(result, dict) else [],
            "body_sha256": guard.digest(result.get("body")) if isinstance(result, dict) else None,
            "body_degraded": result.get("body", {}).get("degraded") if isinstance(result, dict) and isinstance(result.get("body"), dict) else None,
            "source_gate": str(scope_dir / "compiler-001/source-gate.json"),
            "h3_submitted": False,
        })
except Exception as exc:
    failure = f"{type(exc).__name__}: {exc}"

summary = {
    "job_id": JOB.name,
    "compile_status": "PASS" if failure is None else "BLOCKED_OR_FAILED",
    "failure": failure,
    "receipt": str(receipt_file) if receipt_file else None,
    "evidence_dir": str(Path(receipt_file).parent) if receipt_file else None,
    "source_gate": None,
    "h3_submitted": False,
}
if receipt_file:
    gate_path = Path(receipt_file).parent / "compiler-001/source-gate.json"
    if gate_path.is_file():
        gate = json.loads(gate_path.read_text(encoding="utf-8"))
        summary["source_gate"] = {
            "status": gate.get("status"),
            "source": gate.get("source"),
            "required_source": gate.get("required_source"),
            "fallback_reason": gate.get("fallback_reason"),
            "validation_rules": gate.get("validation_rules"),
            "error": gate.get("error"),
            "path": str(gate_path),
        }
print(json.dumps(summary, ensure_ascii=False, indent=2))
if failure is not None:
    sys.exit(2)