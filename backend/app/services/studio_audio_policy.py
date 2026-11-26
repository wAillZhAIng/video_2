from sqlalchemy import update

from app.core.errors import AppError
from app.models import Project
from app.models.product_library import ProjectProductionPreference

def get_preferences(session,project_id):
    if not session.get(Project,project_id):raise AppError(404,'project_missing','项目不存在')
    row=session.get(ProjectProductionPreference,project_id)
    return {'project_id':project_id,'revision':row.revision if row else 0,
            'dialogue_audio_policy':row.dialogue_audio_policy if row else 'auto',
            'reference_video_defaults':list(row.reference_video_defaults_json or []) if row else []}

def get_audio_policy(session,project_id):return get_preferences(session,project_id)['dialogue_audio_policy']

def save_preferences(session,project_id,payload,*,commit=True):
    current=get_preferences(session,project_id)
    if current['revision']!=payload.expected_revision:raise AppError(409,'production_preferences_conflict','项目制作设置已更新，请刷新后保存')
    if payload.dialogue_audio_policy is None and payload.reference_video_defaults is None:
        raise AppError(422,'production_preferences_empty','请至少保存一个制作偏好')
    row=session.get(ProjectProductionPreference,project_id)
    updates={}
    if payload.dialogue_audio_policy is not None:updates['dialogue_audio_policy']=payload.dialogue_audio_policy
    if payload.reference_video_defaults is not None:
        updates['reference_video_defaults_json']=[item.model_dump(exclude_none=True) for item in payload.reference_video_defaults]
    if row:
        changed=session.execute(update(ProjectProductionPreference).where(ProjectProductionPreference.id==project_id,ProjectProductionPreference.revision==payload.expected_revision).values(**updates,revision=ProjectProductionPreference.revision+1))
        if changed.rowcount!=1:raise AppError(409,'production_preferences_conflict','制作设置已更新')
    else:session.add(ProjectProductionPreference(id=project_id,dialogue_audio_policy=updates.get('dialogue_audio_policy','auto'),
                                                  reference_video_defaults_json=updates.get('reference_video_defaults_json',[])))
    if commit:session.commit()
    else:session.flush()
    return get_preferences(session,project_id)
