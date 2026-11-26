from fastapi import APIRouter,Depends,File,Form,UploadFile,Request
from sqlalchemy.orm import Session
from app.core.db import get_db
from app.core.errors import AppError
from app.schemas_product_library import ProductCreate,ProductEdit,ProductImagesEdit,ShotProductReferencesEdit,ExistingProjectProductBind,ProjectProductReferenceEdit
from app.services import product_library as service
from app.services.studio_project_guard import project_guard
from app import models as m
from sqlalchemy import select

router=APIRouter(prefix='/api/product-library',tags=['product-library'])


def traced_selection(request,session,object_id,image_ids,save):
    from app.services.asset_reference_audit import request_trace_id,audit_reference_stage
    trace_id=request_trace_id(request)
    refs=[]
    for image_id in dict.fromkeys(image_ids):
        image=session.get(m.ProductImage,image_id)
        asset=session.get(m.ScreenplayInputAsset,image.asset_id) if image else None
        refs.append({'image_id':image_id,'product_id':image.product_id if image else None,
                     'sha256':asset.sha256 if asset else None,'source_path':asset.relative_path if asset else None})
    try:
        result=save()
    except AppError as error:
        session.rollback()
        audit_reference_stage(session,trace_id,'api_reference_selection_failed',refs,object_id=object_id,error=error)
        session.commit()
        raise
    audit_reference_stage(session,trace_id,'reference_selection_persisted',refs,object_id=object_id,
                          client_image_count=request.headers.get('x-reference-image-count'))
    session.commit()
    return result


@router.get('/projects/{project_id}/references')
def project_references(project_id:str,product_id:str,session:Session=Depends(get_db)):
    return service.project_references(session,project_id,product_id)

@router.put('/projects/{project_id}/references')
def save_project_references(project_id:str,payload:ProjectProductReferenceEdit,request:Request,session:Session=Depends(get_db)):
    return traced_selection(request,session,project_id,payload.image_ids,
        lambda:service.save_project_references(session,request.app.state.settings,project_id,payload))

@router.get('/projects/{project_id}/shots/{shot_id}/references')
def selected_references(project_id:str,shot_id:str,session:Session=Depends(get_db)):
    return service.reference_selection(session,project_id,shot_id)
@router.put('/projects/{project_id}/shots/{shot_id}/references')
async def save_selected_references(project_id:str,shot_id:str,payload:ShotProductReferencesEdit,request:Request,session:Session=Depends(get_db)):
    async with project_guard(request.app,project_id):
        session.expire_all()
        return traced_selection(request,session,shot_id,
            [identifier for product in payload.products for identifier in product.video_image_ids+product.keyframe_image_ids],
            lambda:service.save_reference_selection(session,project_id,shot_id,payload,settings=request.app.state.settings))
@router.get('')
def listing(q:str='',category:str='',include_archived:bool=False,session:Session=Depends(get_db)):
    return service.list_products(session,q,category,include_archived)
@router.post('',status_code=201)
def create(payload:ProductCreate,session:Session=Depends(get_db)):
    return service.create_product(session,payload)
@router.post('/projects/{project_id}/bind')
def bind_existing(project_id:str,payload:ExistingProjectProductBind,request:Request,session:Session=Depends(get_db)):
    return service.bind_existing_project(session,request.app.state.settings,project_id,payload)
@router.get('/{identifier}')
def detail(identifier:str,session:Session=Depends(get_db)):
    return service.dto(session,service.get_product(session,identifier))
@router.patch('/{identifier}')
def edit(identifier:str,payload:ProductEdit,session:Session=Depends(get_db)):
    return service.edit_product(session,identifier,payload)
@router.post('/{identifier}/reanalyze')
def reanalyze(identifier:str,request:Request,session:Session=Depends(get_db)):
    return service.reanalyze(session,request.app.state.settings,identifier)
@router.post('/{identifier}/restore')
def restore(identifier:str,request:Request,session:Session=Depends(get_db)):
    product=service.get_product(session,identifier)
    if product.state!='archived':raise AppError(409,'product_not_archived','只有已移出图库的产品需要恢复')
    service.reanalyze(session,request.app.state.settings,identifier)
    product=service.get_product(session,identifier)
    note=session.scalar(select(m.ContentNote).where(m.ContentNote.resource=='product_file_analysis',m.ContentNote.record_id==identifier))
    import json
    analysis=json.loads(note.notes) if note else {}
    product.state='needs_attention' if analysis.get('status')=='needs_attention' and analysis.get('images') else 'active'
    product.revision+=1;session.commit();return service.dto(session,product)
@router.post('/{identifier}/images',status_code=201)
async def upload(identifier:str,request:Request,files:list[UploadFile]=File(...),purpose:str=Form('other'),notes:str=Form(''),session:Session=Depends(get_db)):
    import hashlib
    from app.services.asset_reference_audit import request_trace_id,audit_reference_stage
    trace_id=request_trace_id(request)
    settings=request.app.state.settings
    if not 1<=len(files)<=32:raise AppError(422,'product_file_count','每批请选择1至32张图片')
    added=[];duplicates=[];errors=[];received=[]
    for ordinal,file in enumerate(files,1):
        try:
            data=await file.read(service.LIMITS['max_upload_bytes']+1)
            received.append({'image_id':f'upload-{ordinal}','source_path':file.filename,
                             'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)})
            value,duplicate=service.add_image(session,settings,identifier,file.filename or '',data,purpose,notes)
            (duplicates if duplicate else added).append(value)
        except AppError as error:
            session.rollback();errors.append({'filename':file.filename,'code':error.code,'error':error.message,'error_type':type(error).__name__})
        finally:await file.close()
    audit_reference_stage(session,trace_id,'api_upload_received',received,object_id=identifier,
                          client_image_count=request.headers.get('x-reference-image-count'))
    audit_reference_stage(session,trace_id,'asset_persistence',
        [{'image_id':item['id'],'sha256':item['sha256'],'source_path':item['filename'],
          'width':item['width'],'height':item['height']} for item in added+duplicates],object_id=identifier,
        errors=errors,received_image_count=len(files))
    session.commit()
    return {'product':service.dto(session,service.get_product(session,identifier)),'added':added,'duplicates':duplicates,'errors':errors,'trace_id':trace_id}
@router.patch('/{identifier}/images')
def images_edit(identifier:str,payload:ProductImagesEdit,session:Session=Depends(get_db)):
    return service.edit_images(session,identifier,payload)
@router.get('/{identifier}/references')
def references(identifier:str,session:Session=Depends(get_db)):
    return service.references(session,identifier)
@router.delete('/{identifier}')
def archive(identifier:str,mode:str='archive',session:Session=Depends(get_db)):
    product=service.get_product(session,identifier)
    if mode!='archive':raise AppError(409,'product_delete_preview_required','删除记录和磁盘文件需先预览影响并明确确认',{'resource':'products','id':identifier,'references':service.references(session,identifier)})
    product.state='archived';product.revision+=1;session.commit();return service.dto(session,product)

@router.delete('/{identifier}/project-bindings/{binding_id}')
def unbind_project(identifier:str,binding_id:str,session:Session=Depends(get_db)):
    binding=session.get(m.ProjectProductBinding,binding_id)
    if not binding or binding.product_id!=identifier:raise AppError(404,'product_binding_missing','产品项目引用不存在')
    active={'queued','submitting','submitted','running','postprocessing','cancelling'}
    for model in (m.GenerationJob,m.ProductionVisual,m.MediaProcessTask):
        if session.scalar(select(model.id).where(model.project_id==binding.project_id,model.status.in_(active)).limit(1)):
            raise AppError(409,'product_binding_busy','该项目仍有制作任务，待结束后解除引用')
    from app.services.studio_pipeline import latest_run,RUN_TERMINAL
    run=latest_run(session,binding.project_id)
    if run and run.status not in RUN_TERMINAL|{'paused','exceptions'}:raise AppError(409,'product_binding_busy','项目自动制作仍在运行')
    for shot in session.scalars(select(m.ShotProductBinding).where(m.ShotProductBinding.project_id==binding.project_id,m.ShotProductBinding.product_id==identifier)):session.delete(shot)
    session.delete(binding);session.commit()
    return {'unlinked':True,'product_id':identifier,'original_files_retained':True,'references':service.references(session,identifier)['references']}
