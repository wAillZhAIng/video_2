"""Existing Comfy Python + official Real-ESRGAN. No implicit model downloads."""
import hashlib,json,os
from pathlib import Path
from app.core.config import PROJECT_ROOT

REALESRGAN_MODEL = 'RealESRGAN_x4plus'
PORTRAIT_MODEL = 'realesr-general-x4v3'
REALESRGAN_ORIGIN = 'http://127.0.0.1:18188'
PORTRAIT_WEIGHT_SHA256 = '8dc7edb9ac80ccdc30c3a5dca6616509367f05fbc184ad95b731f05bece96292'
MODELS = (REALESRGAN_MODEL, PORTRAIT_MODEL)

def matches_service(provider, model_id, base_url):
    return (provider == 'comfyui' and model_id in MODELS
            and (base_url or '').rstrip('/') == REALESRGAN_ORIGIN)

def runtime(settings, model_id=REALESRGAN_MODEL):
    portrait = model_id == PORTRAIT_MODEL
    default_python = '/venv/main/bin/python'
    if not Path(default_python).is_file():
        default_python = 'D:/Dev/Python/Envs/ComfyUI-Independent-20261011/Scripts/python.exe'
    return {'python':os.getenv('VIDEO_FACTORY_UPSCALE_PYTHON', default_python),
        'model':str(settings.media_root.parent/'comfyui_runtime/models/upscale_models'/f'{model_id}.pth'),
        'script':str(PROJECT_ROOT/'scripts/upscale_video_realesrgan.py'),
        'verification':str(settings.media_root.parent/'super_resolution'/('verified-portrait.json' if portrait else 'verified.json'))}

def _model_status(settings, model_id):
    paths=runtime(settings, model_id)
    missing=[key for key in ['python','model','script'] if not Path(paths[key]).is_file()]
    result={'state':'not_configured' if missing else 'detected_not_tested','model':model_id,
        'method':'image_model_applied_to_every_video_frame','temporal_model':False,
        'verified_resolutions':[],'verified_aspect_ratios':[],'missing':missing,'runtime':paths,
        'limits':['图像模型逐帧处理，不提供时序一致性保证','需检查闪烁、伪造细节与过锐化','不得称原生4K生成']}
    if missing:return result
    model_sha=hashlib.sha256(Path(paths['model']).read_bytes()).hexdigest();result['model_sha256']=model_sha
    if model_id == PORTRAIT_MODEL and model_sha != PORTRAIT_WEIGHT_SHA256:
        result.update(state='not_configured', missing=['portrait_weight_sha256_mismatch'])
        return result
    script_sha=hashlib.sha256(Path(paths['script']).read_bytes()).hexdigest();result['script_sha256']=script_sha
    try:
        evidence=json.loads(Path(paths['verification']).read_text('utf-8'))
        if not isinstance(evidence, dict):
            return result
        aspect_ratio=evidence.get('aspect_ratio','16:9')
        if model_id == PORTRAIT_MODEL and (aspect_ratio,evidence.get('width'),evidence.get('height')) != ('9:16',2160,3840):
            return result
        output=(settings.media_root/evidence['relative_path']).resolve()
        if evidence.get('model_sha256')==model_sha and evidence.get('script_sha256')==script_sha and evidence.get('full_decode')==0 and evidence.get('all_frames_processed') and output.is_relative_to(settings.media_root.resolve()) and output.is_file() and hashlib.sha256(output.read_bytes()).hexdigest()==evidence.get('sha256'):
            result.update(state='verified',verified_resolutions=['4k'],verified_aspect_ratios=[aspect_ratio],evidence=evidence)
    except (OSError,ValueError,KeyError):pass
    return result


def status(settings, model_id=None):
    if model_id is not None:
        if model_id not in MODELS:
            raise ValueError('Unsupported Real-ESRGAN model')
        return _model_status(settings, model_id)
    profiles = {model: _model_status(settings, model) for model in MODELS}
    landscape, portrait = profiles[REALESRGAN_MODEL], profiles[PORTRAIT_MODEL]
    by_aspect = {aspect: model for model, profile in profiles.items() for aspect in profile['verified_aspect_ratios']}
    # Keep the legacy payload fields for callers that read one runtime or proof.
    primary = landscape
    if ((landscape['state'] != 'verified' and portrait['state'] == 'verified')
            or (landscape['state'] == 'not_configured' and portrait['state'] != 'not_configured')):
        primary = portrait
    return {**primary, 'verified_resolutions':['4k'] if by_aspect else [], 'verified_aspect_ratios':list(by_aspect),
            'model_by_aspect_ratio':by_aspect, 'models':profiles}


def target_status(aggregate, aspect_ratio, model_id=None, *, verification_run=False):
    """Select one model, so fingerprints and proof never cross model identities."""
    if not aggregate or 'models' not in aggregate:
        return aggregate
    profiles = aggregate['models']
    if model_id is not None:
        return profiles.get(model_id)
    portrait = profiles[PORTRAIT_MODEL]
    if verification_run and aspect_ratio == '9:16' and portrait['state'] != 'not_configured':
        return portrait
    verified_model = aggregate['model_by_aspect_ratio'].get(aspect_ratio)
    return profiles[verified_model] if verified_model else profiles[REALESRGAN_MODEL]
