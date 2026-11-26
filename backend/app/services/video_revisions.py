"""Resolve text revisions from native generation inputs, never upscale metadata."""
import re
from app.core.errors import AppError
from app.models import GenerationJob
from app.schemas import GenerationRequest


def revision_reference_digest(references, service, generation, source_video_id):
    """Digest the exact user-selected native revision inputs for preview/submit."""
    from app.services.cinematic_rules import fingerprint

    return fingerprint({
        'source_video_id': source_video_id,
        'references': [{key: value for key, value in ref.items() if key != 'absolute_path'} for ref in references],
        'service_id': service['service_id'], 'service_revision': service['service_revision'],
        'model_id': service['model_id'], 'parameters': generation.parameters,
        'prompt_override': generation.prompt_override, 'seed': generation.seed,
    })


def revision_source_job(session, shot, current):
    previous = session.get(GenerationJob, current.generation_job_id)
    if previous is None:
        raise AppError(404, 'revision_source_job_missing', '原视频的生成任务不存在，无法读取修订参数')
    if (current.project_id != shot.project_id or current.shot_id != shot.id
            or previous.project_id != shot.project_id or previous.shot_id != shot.id):
        raise AppError(409, 'revision_source_scope', '原视频及生成任务必须属于当前项目和分镜')
    return previous


def revision_generation_request(session, shot, current, payload):
    previous = revision_source_job(session, shot, current)
    original = previous.parameters_json or {}
    snapshot = original.get('service_snapshot') or {}
    seed = payload.seed if payload.seed is not None else previous.seed
    if payload.use_project_video_service:
        if (payload.service_id or payload.model_id or payload.parameters.get('service_id')
                or payload.parameters.get('model_id')):
            raise AppError(422, 'revision_service_conflict', '请在沿用项目制作设置与指定服务之间选择一种')
        # The selected H3 workflow supplies native dimensions and sampler steps.
        return GenerationRequest(seed=seed, parameters=payload.parameters, prompt_override=payload.prompt_override)
    # Copy only native sampler settings. Old Studio IDs, reference manifests and
    # screenplay snapshots must be rebuilt for the new task by generate_for_shot.
    parameters = {k: original[k] for k in ('width', 'height', 'steps') if k in original}
    if not any(k in payload.parameters for k in ('width', 'height')):
        width, height = parameters.get('width'), parameters.get('height')
        ratio = {'16:9': 16/9, '9:16': 9/16, '1:1': 1}[shot.aspect_ratio]
        if (not isinstance(width, int) or not isinstance(height, int)
                or width <= 0 or height <= 0 or abs(width / height - ratio) > .025):
            from app.services.video_quality import plan
            native = plan({'aspect_ratio': shot.aspect_ratio})['generation']
            parameters.update(width=native['width'], height=native['height'])
    parameters.update(payload.parameters)
    # Explicit provider/model selection wins; changing services must not silently
    # force that service to load the previous service's unrelated model.
    service_id = payload.service_id or parameters.get('service_id') or snapshot.get('service_id')
    model_id = payload.model_id or parameters.get('model_id')
    if not model_id and service_id == snapshot.get('service_id'):
        model_id = snapshot.get('requested_model_id') or snapshot.get('model_id')
    return GenerationRequest(seed=seed,
                             parameters=parameters, service_id=service_id, model_id=model_id, prompt_override=payload.prompt_override)


def prepare_native_revision(session, settings, shot, current, payload):
    """Prepare direct H3 revision inputs without a compiler or auxiliary VLM."""
    from app.api.routes import script_for_shot
    from app.services.revision_references import resolve_revision_images
    from app.services.video_services import select_video_service

    previous = revision_source_job(session, shot, current)
    script = script_for_shot(session, shot)
    if not script or script.project_id != shot.project_id or script.status != 'approved':
        raise AppError(409, 'script_not_approved', '脚本尚未审核通过')
    if shot.status != 'approved':
        raise AppError(409, 'shot_not_approved', '分镜尚未审核通过')
    generation = revision_generation_request(session, shot, current, payload)
    service_id, model_id = generation.service_id, generation.model_id
    if not payload.use_project_video_service and previous.provider.startswith('cloud_') and not service_id:
        raise AppError(409, 'revision_cloud_service_missing', '原视频缺少可用的云端服务快照，请选择当前项目视频服务或明确指定云端服务后重试')
    _, service, provider = select_video_service(session, settings, shot.project_id, service_id, model_id)
    selections = [item.model_dump() for item in payload.reference_images]
    references = resolve_revision_images(session, settings, shot, current.id, selections) if selections else []
    if references and provider.name != 'comfyui_h3':
        raise AppError(409, 'revision_image_native_h3_required',
                       '带图片的成片修订需要本机 MiniMax H3 原生参考图服务；未将图片转换为文字或发送到其他服务')
    return generation, service, references, revision_reference_digest(references, service, generation, current.id)


def apply_revision_instruction(prompt, instruction, *, structured, include_unstructured=False):
    if not instruction:
        return prompt
    if structured:
        heading = 'detailed_description:\n'
        return (prompt.replace(heading, heading+'Reviewed repair direction for this shot: '+instruction+'\n', 1)
                if heading in prompt else prompt+'\nRevision instruction: '+instruction)
    return prompt+'\nRevision instruction: '+instruction if include_unstructured else prompt


def cloud_revision_targets(session, snapshot, references):
    """Use frozen entity identities/names; never merge distinct products."""
    from app import models as m
    targets = []
    for entity in (snapshot or {}).get('cinematic', {}).get('entities', []):
        if entity.get('kind') != 'prop':
            continue
        target = {key: entity.get(key) for key in ('id', 'logical_id', 'kind')}
        target['name'] = entity.get('name') or entity.get('spec', {}).get('name')
        current = session.get(m.ProductionEntity, entity['id']) if entity.get('id') else None
        if (current and current.kind == entity.get('kind') and current.logical_id == entity.get('logical_id')
                and current.spec_json == entity.get('spec')):
            target['name'] = current.name
        targets.append(target)
    if targets:
        return targets
    products = {}
    for reference in references:
        if reference.get('role') != 'product' or not reference.get('product_id'):
            continue
        identifier = reference['product_id']
        products[identifier] = {'id': identifier, 'logical_id': reference.get('logical_id') or reference.get('product_name') or identifier,
                                'name': reference.get('product_name') or identifier, 'kind': 'prop'}
    return list(products.values())


def editable_generation_prompt(prompt, instruction, *, structured, prompt_override=None, targets=()):
    """Use exactly the edited text while retaining a separately entered revision.

    Legacy build_prompt already includes the revision. Structured prompts insert
    it here; an override replaces the compiled body in either path.
    """
    declared_body = prompt_override if prompt_override is not None else prompt
    if prompt_override is not None:
        if not prompt_override.strip():
            raise AppError(422, 'prompt_override_empty', '自定义提示词不能为空')
        prompt = prompt_override
    prompt = apply_revision_instruction(prompt, instruction, structured=structured,
                                        include_unstructured=prompt_override is not None)
    # Providers receive this body directly.  Give explicit current scale the
    # same priority as the authored revision without rewriting it through an
    # auxiliary model.
    from app.services.revision_constraints import extract_revision_constraints, revision_constraints_text
    constraints = extract_revision_constraints(instruction, prompt_override=prompt_override, targets=targets)
    for item in constraints.get('items', []):
        key = item.get('target_key') or ''
        declared = [target for target in targets if key in {str(target.get(k)) for k in ('id', 'logical_id', 'name')}]
        # Without a structured target, an explicit code must also occur in the
        # original/edited scene body. Placeholder targets never reach a model.
        explicit = bool(re.fullmatch(r'P\d{1,6}', key, re.I)
                        and re.search(r'(?<![A-Za-z0-9_])'+re.escape(key)+r'(?![A-Za-z0-9_])', declared_body, re.I))
        if key.startswith('__') or len(declared) > 1 or (not declared and not explicit):
            raise AppError(422, 'revision_scale_target_ambiguous', '尺寸修改需要明确唯一商品，请写出该镜头的商品名称或P编号；多个商品不能共用一个尺寸占位符')
    priority = revision_constraints_text(constraints)
    if priority:
        priority += ('\nThese current physical-scale requirements override conflicting dimensions in the original '
                     'shot, reference captions and bound prop descriptions. Preserve only the unchanged details.\n')
        heading = 'detailed_description:\n'
        if structured and heading in prompt:
            prompt = prompt.replace(heading, heading + priority, 1)
        elif prompt_override is not None:
            # The editable prompt is caller-authored and must remain byte-for-
            # byte at the beginning. The explicit priority still overrides its
            # superseded dimensions rather than silently truncating that text.
            prompt += '\n' + priority
        else:
            prompt = priority + prompt
    return prompt
