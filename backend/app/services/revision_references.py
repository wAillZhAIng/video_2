"""Private source-video-scoped uploads used only by an explicit review revision."""
from hashlib import sha256
from pathlib import Path

from sqlalchemy import select

from app import models as m
from app.core.errors import AppError
from app.core.paths import safe_media_path
from app.models.entities import new_id
from app.services.cinematic_quality import production_context
from app.services.identity_references import verified_image
from app.services.screenplay_generation_media import inspect_image_upload


def revision_targets(session, shot):
    targets = {'character': {}, 'product': {}}
    cast = list(session.scalars(select(m.ShotCharacter.character_id).where(
        m.ShotCharacter.shot_id == shot.id).order_by(m.ShotCharacter.order, m.ShotCharacter.id)))
    for identifier in dict.fromkeys([*cast, shot.character_id]):
        character = session.get(m.Character, identifier) if identifier else None
        if character:
            targets['character'][identifier] = {'id': identifier, 'name': character.display_name}
    _, _, _, _, entities = production_context(session, shot)
    for entity in entities:
        if entity and entity.kind == 'prop' and entity.project_id == shot.project_id:
            targets['product'][entity.id] = {'id': entity.id, 'name': entity.name, 'logical_id': entity.logical_id}
    linked = session.execute(select(m.ProjectProductBinding, m.ShotProductBinding.product_id).join(
        m.ShotProductBinding, m.ShotProductBinding.product_id == m.ProjectProductBinding.product_id).where(
        m.ProjectProductBinding.project_id == shot.project_id, m.ShotProductBinding.project_id == shot.project_id,
        m.ShotProductBinding.shot_id == shot.id).order_by(m.ShotProductBinding.product_id))
    for binding, identifier in linked:
        name = binding.snapshot_json.get('name') or identifier
        targets['product'][identifier] = {'id': identifier, 'name': name}
    return targets


def store_revision_image(session, settings, video, kind, filename, data):
    filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
    extension = Path(filename).suffix.lower()
    details = inspect_image_upload(data, extension)
    identifier = new_id('revimg')
    relative_path = f'revision_references/{video.project_id}/{video.shot_id}/{video.id}/{identifier}{extension}'
    path = safe_media_path(settings.media_root, relative_path)
    record = m.ManagedFile(id=identifier, relative_path=relative_path, state='active',
        ownership_json=[{'table': 'video_revisions', 'id': video.id}],
        metadata_json={**details, 'kind': 'video_revision_reference', 'reference_kind': kind,
                       'filename': filename[:255], 'sha256': sha256(data).hexdigest(), 'verified_owned': True})
    written = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('xb') as stream:
            written = True
            stream.write(data)
        session.add(record)
        session.commit()
    except Exception:
        session.rollback()
        if written:
            path.unlink(missing_ok=True)
        raise
    return {'id': identifier, 'kind': kind, 'filename': record.metadata_json['filename'],
            'media_url': '/api/media/' + relative_path}


def resolve_revision_images(session, settings, shot, source_video_id, selections):
    video = session.get(m.VideoRevision, source_video_id)
    if not video or video.project_id != shot.project_id or video.shot_id != shot.id:
        raise AppError(409, 'revision_reference_scope', '修订图片只可用于原来的视频版本和分镜')
    targets = revision_targets(session, shot)
    references = []
    hashes = set()
    for selection in selections:
        identifier = selection['image_id']
        record = session.get(m.ManagedFile, identifier)
        metadata = record.metadata_json if record else {}
        kind = metadata.get('reference_kind')
        if (not record or record.state != 'active' or metadata.get('kind') != 'video_revision_reference'
                or kind not in targets or {'table': 'video_revisions', 'id': video.id} not in record.ownership_json):
            raise AppError(409, 'revision_reference_scope', '修订图片已移除或属于其他视频，请重新添加')
        target_id = selection.get('target_id')
        target = targets[kind].get(target_id) if target_id else None
        if target_id and target is None:
            raise AppError(409, 'revision_reference_target', '所选图片的应用对象已变化，请重新选择')
        digest = metadata['sha256']
        if digest in hashes:
            raise AppError(422, 'revision_reference_duplicate', '本次修订重复添加了相同的图片')
        hashes.add(digest)
        ref = {'asset_id': identifier, 'asset_type': 'revision_' + kind + '_image',
               'source': 'video_revision_reference', 'role': 'identity' if kind == 'character' else 'product',
               'target_id': target_id, 'relative_path': record.relative_path, 'sha256': digest,
               'filename': metadata['filename'], 'target_name': target['name'] if target else None}
        if kind == 'character':
            ref['character_id'] = target_id or identifier
        else:
            ref['product_name'] = target['name'] if target else f'附加道具商品图 {len(references) + 1}'
            if target:
                ref['product_id'] = target_id
                if target.get('logical_id'):
                    ref['product_logical_id'] = target['logical_id']
        ref['absolute_path'] = str(verified_image(settings, ref))
        references.append(ref)
    return references


def replaced_targets(references, role):
    return {ref['target_id'] for ref in references if ref['role'] == role and ref.get('target_id')}


def revision_image_evidence(ref):
    result = {key: value for key, value in ref.items() if key not in {'absolute_path', 'picture_index', 'constraint_target_keys'}}
    if ref.get('role') == 'product' and not ref.get('target_id') and ref.get('product_id') == ref['asset_id']:
        # Earlier review snapshots stored the upload ID as a synthetic product ID.
        result.pop('product_id')
    return result


def revision_image_preview(ref, index):
    return {'id': ref['asset_id'], 'kind': 'character' if ref['role'] == 'identity' else 'product',
            'filename': ref['filename'], 'target_name': ref['target_name'],
            'picture_index': index, 'media_url': '/api/media/' + ref['relative_path']}
