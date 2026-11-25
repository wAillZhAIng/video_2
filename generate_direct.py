"""
人物一致性 直通批量生成脚本
架构：MiniMaxH3ReferenceToVideo 直通，完全绕过 VLM 编译层
ComfyUI API: http://127.0.0.1:8189
输入目录:    D:\人物一致性\.local-runtime\inference\input
输出目录:    D:\人物一致性\.local-runtime\inference\output\direct\
"""
import copy, json, random, sys, time, urllib.request, urllib.error
from pathlib import Path

BASE        = Path(r"D:\人物一致性\.local-runtime\inference")
INPUT_DIR   = BASE / "input"
API         = "http://127.0.0.1:8189"
OUTPUT_PFX  = "direct"

T2V = json.loads((BASE / "t2v.json").read_text(encoding="utf-8"))
I2V = json.loads((BASE / "i2v.json").read_text(encoding="utf-8"))
R2V = json.loads((BASE / "r2v.json").read_text(encoding="utf-8"))

# ---------------------------------------------------------------------------
# 产品核心描述 — 所有提示词共享，确保模型始终知道产品的精确外观
# ---------------------------------------------------------------------------
PRODUCT_CORE = (
    "Black dual-head children's bicycle tail light, two lamp heads side by side, "
    "solid matte black body, mounted under the seat post. "
    "When signaling, one side glows orange and projects a bright orange double-arrow "
    "pattern on the asphalt. "
    "Product shape, proportions, color and markings must stay identical to the "
    "reference image in every frame. Never alter the product size, structure or color."
)

# ---------------------------------------------------------------------------
# 场景定义 — 每场景一句话 intent，参考图按槽位指定
# 图片文件名对应 D:\人物一致性\.local-runtime\inference\input\ 下的文件
# ---------------------------------------------------------------------------
SCENES = [
    {
        "name": "scene01_police",
        "workflow": "t2v",
        "frames": 120,
        "prompt": (
            "Dusk North American suburban street. "
            "A little girl in purple tracksuit and pink helmet rides a pink children's bicycle. "
            "The tail light under the seat projects bright orange double-arrow patterns on the road — "
            "left side flashing, right side steady. "
            "Three Western male police officers surround her; one kneels pointing at the orange "
            "arrow projection, another leans over in amazement. Police car with flashing lights in background. "
            "Handheld slight shake, medium close-up pushing slowly toward the light projection. "
            + PRODUCT_CORE
        ),
    },
    {
        "name": "scene02_unbox",
        "workflow": "i2v",
        "frames": 48,
        "first_frame": "product_main.jpg",
        "prompt": (
            "First-person POV looking down at an outdoor wooden table. "
            "Both hands open a black cardboard box printed with 'TURNSIGNAL' and orange double-arrow graphics. "
            "Inside, a black dual-head bicycle tail light is revealed — two lamp heads side by side, "
            "matte black body, orange LED lenses clearly visible. "
            "Hands lift it toward the camera to show the lamp bead details. "
            "Natural outdoor light, steady fixed shot. "
            + PRODUCT_CORE
        ),
    },
    {
        "name": "scene03_install",
        "workflow": "i2v",
        "frames": 24,
        "first_frame": "product_main.jpg",
        "prompt": (
            "First-person POV looking down at a pink children's bicycle seat post. "
            "Daytime beside a playground. Both hands clip the black dual-head tail light "
            "onto the seat post clamp, then release to show the installed position — "
            "two lamp heads pointing rearward, body seated flat and flush. "
            "Steady fixed shot, clean natural light. "
            + PRODUCT_CORE
        ),
    },
    {
        "name": "scene04_night_right",
        "workflow": "r2v",
        "frames": 48,
        "pic1": "char_ref.png",
        "pic2": "product_right_arrow.jpg",
        "prompt": (
            "Night suburban two-lane road. "
            "A little girl in pink jacket and pink helmet rides a pink bicycle forward, about to turn right. "
            "The right lamp head glows orange, projecting a flashing bright orange double-right-arrow "
            "pattern on the dark asphalt to the right of the rear wheel. "
            "A red SUV passes on the right. "
            "Camera follows from directly behind at the same speed, slight cycling wobble, medium close-up. "
            "Use <Picture 2> for exact product appearance — same shape, color and arrow projection. "
            + PRODUCT_CORE
        ),
    },
    {
        "name": "scene05_night_left_change",
        "workflow": "r2v",
        "frames": 48,
        "pic1": "char_ref.png",
        "pic2": "product_left_arrow.jpg",
        "prompt": (
            "Night busy multi-lane urban boulevard with high-rise buildings. "
            "A little girl in sky-blue top and grey patterned helmet rides a dark purple bicycle "
            "in traffic, preparing to change lanes left. "
            "The left lamp head glows orange, projecting a flashing bright orange double-left-arrow "
            "pattern on the asphalt to the left of the rear wheel. "
            "Several cars ahead with red tail lights. "
            "Camera tracks from the right rear diagonally. "
            "Use <Picture 2> for exact product appearance — same shape, color and arrow projection. "
            + PRODUCT_CORE
        ),
    },
    {
        "name": "scene06_night_intersection",
        "workflow": "r2v",
        "frames": 72,
        "pic1": "char_ref.png",
        "pic2": "product_left_arrow.jpg",
        "prompt": (
            "Night urban high-rise intersection on a straight road. "
            "A little girl in light-yellow jacket and pink helmet rides a pink bicycle "
            "approaching a crosswalk and traffic lights. "
            "The left lamp head continuously flashes orange, projecting a bright orange "
            "double-left-arrow pattern on the dark asphalt, warning surrounding vehicles. "
            "Camera follows steadily from directly behind, smooth tracking shot through the intersection. "
            "Use <Picture 2> for exact product appearance — same shape, color and arrow projection. "
            + PRODUCT_CORE
        ),
    },
]

# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def api(path, data=None):
    url = API + path
    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"} if body else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail[:800]}")


def wait_for_prompt(prompt_id, scene_name):
    print(f"  [{scene_name}] submitted, waiting...", flush=True)
    dot = 0
    while True:
        time.sleep(8)
        hist = api(f"/history/{prompt_id}")
        if prompt_id in hist:
            status = hist[prompt_id].get("status", {})
            if status.get("completed"):
                outputs = hist[prompt_id].get("outputs", {})
                files = []
                for node_out in outputs.values():
                    for v in node_out.get("videos", []):
                        files.append(v.get("filename", ""))
                    for v in node_out.get("images", []):
                        files.append(v.get("filename", ""))
                return files
            if status.get("status_str") in ("error", "failed"):
                msgs = status.get("messages", [])
                raise RuntimeError(f"ComfyUI error: {msgs}")
        dot += 1
        if dot % 5 == 0:
            print(f"  [{scene_name}] still generating... ({dot*8}s)", flush=True)


def build_workflow(scene):
    wf_name = scene["workflow"]
    if wf_name == "t2v":
        wf = copy.deepcopy(T2V)
    elif wf_name == "i2v":
        wf = copy.deepcopy(I2V)
    else:
        wf = copy.deepcopy(R2V)

    wf["136"]["inputs"]["prompt"] = scene["prompt"]
    wf["136"]["inputs"]["length"] = scene["frames"]
    wf["129"]["inputs"]["noise_seed"] = random.randint(1, 2**31 - 1)
    wf["92"]["inputs"]["filename_prefix"] = f"{OUTPUT_PFX}/{scene['name']}"

    if wf_name == "i2v" and "first_frame" in scene:
        wf["150"]["inputs"]["image"] = scene["first_frame"]

    if wf_name == "r2v":
        wf["150"]["inputs"]["image"] = scene["pic1"]
        wf["151"]["inputs"]["image"] = scene["pic2"]

    return wf


def check_input_images():
    """验证所有场景所需的参考图文件存在."""
    missing = []
    needed = set()
    for s in SCENES:
        if s["workflow"] == "i2v" and "first_frame" in s:
            needed.add(s["first_frame"])
        if s["workflow"] == "r2v":
            needed.add(s["pic1"])
            needed.add(s["pic2"])
    for f in needed:
        p = INPUT_DIR / f
        if not p.exists():
            missing.append(str(p))
    return missing


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    # ComfyUI 健康检查
    try:
        info = api("/system_stats")
        gpu = info.get("devices", [{}])[0].get("name", "?")
        print(f"ComfyUI online  GPU: {gpu}\n")
    except Exception as e:
        print(f"[ERROR] ComfyUI not responding: {e}")
        sys.exit(1)

    # 参考图存在性检查（缺失时仍继续，逐场景报错）
    missing = check_input_images()
    if missing:
        print("[WARN] The following reference images are missing from input dir:")
        for m in missing:
            print(f"  - {m}")
        print("Scenes that require them will fail. Copy the images and retry.\n")

    # 在 input 目录创建 product_main.jpg / product_left_arrow.jpg /
    # product_right_arrow.jpg 的说明文件（如尚未存在）
    readme = INPUT_DIR / "DIRECT_WORKFLOW_README.txt"
    if not readme.exists():
        readme.write_text(
            "Direct workflow reference images needed in this directory:\n"
            "  char_ref.png          — character identity (1344x768)\n"
            "  product_main.jpg      — product white-bg main shot (1344x768)\n"
            "  product_left_arrow.jpg — product with left orange arrow lit (1344x768)\n"
            "  product_right_arrow.jpg — product with right orange arrow lit (1344x768)\n"
            "All images should be cropped/padded to 1344x768 to avoid ref_image_size distortion.\n",
            encoding="utf-8"
        )

    SKIP = set()   # 填入已生成场景名跳过，例如 {"scene01_police"}

    results = []
    for i, scene in enumerate(SCENES, 1):
        if scene["name"] in SKIP:
            print(f"[{i}/{len(SCENES)}] {scene['name']}  skipped")
            continue
        print(f"\n{'='*55}")
        print(f"[{i}/{len(SCENES)}] {scene['name']}  wf={scene['workflow']}  frames={scene['frames']}")
        try:
            wf = build_workflow(scene)
            resp = api("/prompt", {"prompt": wf})
            pid = resp["prompt_id"]
            files = wait_for_prompt(pid, scene["name"])
            results.append((scene["name"], "OK", files))
            print(f"  [OK] output: {files}")
        except Exception as e:
            results.append((scene["name"], f"FAIL: {e}", []))
            print(f"  [FAIL] {e}")

    print(f"\n{'='*55}")
    print("Summary:")
    for name, status, files in results:
        flag = "[OK]" if status == "OK" else "[!!]"
        print(f"  {flag} {name}: {status}  {files}")
    print(f"\nOutput dir: {BASE / 'output' / OUTPUT_PFX}")


if __name__ == "__main__":
    main()
