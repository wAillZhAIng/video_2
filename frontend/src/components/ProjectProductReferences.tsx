import {useEffect,useRef,useState} from 'react';
import {api,request} from '../lib/api';
import type {ProductAsset} from '../lib/asset-library-contract';
import {FormField} from './ui';

type ProductRow={product_id:string;name:string;revision:number;images:{id:string;filename?:string;media_url?:string;thumbnail_url?:string;url?:string;thumbnail?:string;purpose?:string}[];video_image_ids:string[];keyframe_image_ids:string[]};
type Shot={id:string;order:number;title:string};
export type ProjectProductReferenceOverview={project_id:string;shot_id:string;products:ProductRow[];limits:{keyframe_total_max:number;video_total_max:number};note:string};
export type ProductReferenceState={busy:boolean;dirty:boolean;overBudget:boolean};
type ProductReferenceRetry={runId:string;nodeId:string;shotId:string};
type Props={
  projectId:string;
  shots:Shot[];
  disabled:boolean;
  libraryProducts?:ProductAsset[];
  retry?:ProductReferenceRetry;
  onOpenChange?:(open:boolean)=>void;
  onStateChange?:(state:ProductReferenceState)=>void;
  onChanged?:()=>void|Promise<void>;
  onCancel?:()=>void;
};
const idleState:ProductReferenceState={busy:false,dirty:false,overBudget:false};
const hasSelection=(row:ProductRow)=>Boolean(row.revision||row.video_image_ids.length||row.keyframe_image_ids.length);

function selectedIds(rows:ProductRow[]){
  return JSON.stringify(rows.filter(hasSelection).map(row=>[row.product_id,row.video_image_ids,row.keyframe_image_ids]));
}

export function ProjectProductReferences({projectId,shots,disabled,libraryProducts=[],retry,onOpenChange,onStateChange,onChanged,onCancel}:Props){
  const [open,setOpen]=useState(false);
  const [shotId,setShotId]=useState('');
  const [data,setData]=useState<ProjectProductReferenceOverview|null>(null);
  const [drafts,setDrafts]=useState<ProductRow[]>([]);
  const [loading,setLoading]=useState(false);
  const [saving,setSaving]=useState(false);
  const [accepted,setAccepted]=useState(false);
  const [error,setError]=useState('');
  const [notice,setNotice]=useState('');
  const [reload,setReload]=useState(0);
  const selectedShot=retry?.shotId||(shots.some(shot=>shot.id===shotId)?shotId:shots[0]?.id||'');
  const path=`/api/product-library/projects/${encodeURIComponent(projectId)}/shots/${encodeURIComponent(selectedShot)}/references`;
  const current=useRef(path);
  const active=useRef(true);
  const submitting=useRef(false);
  current.current=path;
  const callbacks=useRef({onStateChange,onChanged});
  callbacks.current={onStateChange,onChanged};

  const dirty=Boolean(data)&&!accepted&&selectedIds(drafts)!==selectedIds(data!.products);
  const keyframeCount=drafts.reduce((total,item)=>total+item.keyframe_image_ids.length,0);
  const videoCount=drafts.reduce((total,item)=>total+item.video_image_ids.length,0);
  const overBudget=Boolean(data)&&(videoCount>data!.limits.video_total_max||!retry&&keyframeCount>data!.limits.keyframe_total_max);
  const available=libraryProducts.filter(item=>item.state==='active'&&item.images.length
    &&!drafts.some(product=>product.product_id===item.id));
  const readOpen=open||Boolean(retry);
  useEffect(()=>{
    active.current=true;
    return()=>{active.current=false;callbacks.current.onStateChange?.(idleState);};
  },[]);
  useEffect(()=>{
    if(!readOpen||!selectedShot)return;
    const abort=new AbortController();
    setLoading(true);
    setData(null);
    setDrafts([]);
    setError('');
    setNotice('');
    void request<ProjectProductReferenceOverview>(path,{signal:abort.signal}).then(value=>{
      if(value.project_id!==projectId||value.shot_id!==selectedShot)throw new Error('产品参考响应不属于当前项目和分镜');
      if(!abort.signal.aborted){setData(value);setDrafts(value.products);}
    }).catch(e=>{if(!abort.signal.aborted)setError(String(e));}).finally(()=>{if(!abort.signal.aborted)setLoading(false);});
    return()=>abort.abort();
  },[path,projectId,selectedShot,readOpen,reload]);
  useEffect(()=>{callbacks.current.onStateChange?.({busy:saving,dirty,overBudget});},[saving,dirty,overBudget]);

  function toggle(productId:string,field:'keyframe_image_ids'|'video_image_ids',imageId:string,checked:boolean){
    setDrafts(previous=>previous.map(product=>product.product_id===productId?{
      ...product,[field]:checked?[...product[field],imageId]:product[field].filter(id=>id!==imageId),
    }:product));
    setNotice('');
  }
  function clearReferences(){
    setDrafts(previous=>previous.map(product=>({
      ...product,video_image_ids:[],keyframe_image_ids:retry?product.keyframe_image_ids:[],
    })));
    setNotice('');
  }
  function addProduct(productId:string){
    const selected=available.find(item=>item.id===productId);
    if(!selected||drafts.length>=4)return;
    setDrafts(previous=>[...previous,{product_id:selected.id,name:selected.name,images:selected.images,
      revision:0,video_image_ids:[],keyframe_image_ids:[]}]);
    setNotice('');
  }
  async function save(){
    if(!data||disabled||submitting.current||!dirty||overBudget)return;
    const products=drafts.filter(hasSelection).map(product=>({
      product_id:product.product_id,
      expected_revision:product.revision,
      video_image_ids:product.video_image_ids,
      keyframe_image_ids:product.keyframe_image_ids,
    }));
    const isCurrent=()=>active.current&&current.current===path;
    submitting.current=true;
    setSaving(true);
    setError('');
    setNotice('');
    try{
      if(retry){
        await request(`/api/studio/projects/${encodeURIComponent(projectId)}/retry-product-references`,{
          method:'POST',
          body:JSON.stringify({expected_run_id:retry.runId,node_id:retry.nodeId,products}),
        });
        if(!isCurrent())return;
        setAccepted(true);
        setNotice('已保存视频产品引用并提交此分镜重试。');
      }else{
        const value=await request<ProjectProductReferenceOverview>(path,{method:'PUT',body:JSON.stringify({products})});
        if(!isCurrent())return;
        if(value.project_id!==projectId||value.shot_id!==selectedShot)throw new Error('保存响应不属于当前分镜');
        setData(value);
        setDrafts(value.products);
        setNotice('此分镜的产品参考已保存。');
      }
      try{await callbacks.current.onChanged?.();}catch(e){
        if(isCurrent())setError('保存已完成。工作台刷新失败，请重新读取任务状态。'+String(e));
      }
    }catch(e){
      if(isCurrent())setError(String(e));
    }finally{
      submitting.current=false;
      if(isCurrent())setSaving(false);
    }
  }
  const shot=shots.find(item=>item.id===selectedShot);
  const locked=disabled||saving||accepted;
  const content=<>
    {retry?<>
      <h3>{shot&&shot.order>0?`#${shot.order} · `:null}{shot?.title||selectedShot}</h3>
      <p className="form-hint">检查缩略图，取消有冲突的照片，再勾选同一款商品的清晰照片。可以选多张，也可以取消全部勾选以解除本镜头的视频商品参考。</p>
    </>:<>
      <p className="form-hint">关键画面和视频可分别选图，也可留空。留空的阶段使用剧本和其他可用素材制作。</p>
      {shots.length?<FormField label="选择产品参考分镜"><select value={selectedShot} disabled={locked||dirty} onChange={event=>setShotId(event.target.value)}>
        {shots.map(item=><option key={item.id} value={item.id}>#{item.order} · {item.title}</option>)}
      </select></FormField>:<p>先建立分镜，再选择产品制作参考。</p>}
    </>}
    {loading?<p role="status">读取此镜头的产品资料和已选图片…</p>:null}
    {error?<p role="alert" className="inline-notice warning">{error}</p>:null}
    {data?<>
      {!retry&&available.length?<FormField label="从产品图库添加到本镜">
        <select value="" disabled={locked||drafts.length>=4} onChange={event=>addProduct(event.target.value)}>
          <option value="" disabled>选择产品</option>
          {available.map(item=><option key={item.id} value={item.id}>{item.name}</option>)}
        </select>
      </FormField>:null}
      <p>产品图数量：{!retry?`关键画面 ${keyframeCount} / 总预算 ${data.limits.keyframe_total_max} · `:null}视频 {videoCount} / 总预算 {data.limits.video_total_max}</p>
      <p className="form-hint">{data.note}</p>
      {overBudget?<p role="alert" className="inline-notice warning">当前产品图片已超过模型预算。请减少对应阶段的选择。人物与场景图片也占用总预算。</p>:null}
      {drafts.map(product=><article className="asset-picker-product" key={product.product_id}>
        <h3>{product.name}</h3>
        {product.revision===0?<button type="button" className="button button-ghost button-small" disabled={locked}
          onClick={()=>setDrafts(previous=>previous.filter(row=>row.product_id!==product.product_id))}>移除本镜的{product.name}</button>:null}
        <div className="asset-media-grid">{product.images.map((image,index)=><article className="asset-image-card" key={image.id}>
          <img src={api.mediaUrl(image.thumbnail_url||image.thumbnail||image.media_url||image.url)} alt={`${product.name}制作参考图 ${index+1}`}/>
          <small>{image.filename||image.id} · {image.purpose||'用途见产品图库'}</small>
          {!retry?<label><input type="checkbox" checked={product.keyframe_image_ids.includes(image.id)} disabled={locked} onChange={event=>toggle(product.product_id,'keyframe_image_ids',image.id,event.target.checked)}/>{product.name}图 {index+1} 用于关键画面</label>:null}
          <label><input type="checkbox" checked={product.video_image_ids.includes(image.id)} disabled={locked} onChange={event=>toggle(product.product_id,'video_image_ids',image.id,event.target.checked)}/>{product.name}图 {index+1} 用于视频</label>
        </article>)}</div>
      </article>)}
      {!drafts.length?<p>本镜产品参考为空。</p>:null}
    </>:null}
    <div className="asset-actions">
      {drafts.length?<button type="button" className="button button-primary button-small" disabled={locked||!dirty||overBudget} onClick={()=>void save()}>
        {saving?(retry?'正在保存并提交重试…':'正在保存制作引用…'):retry?'保存并重试此镜头':'保存此镜头的产品参考选择'}
      </button>:null}
      {drafts.length?<button type="button" className="button button-secondary button-small"
        disabled={locked||!(videoCount||(!retry&&keyframeCount))} onClick={clearReferences}>
        {retry?'清空视频产品引用':'清空本镜产品引用'}
      </button>:null}
      <button type="button" className="button button-secondary button-small" disabled={saving||loading||accepted} onClick={()=>{
        if(dirty&&!window.confirm('放弃此镜头尚未保存的产品参考选择并重新读取？'))return;
        setReload(value=>value+1);
      }}>重新读取此镜头</button>
      {retry&&onCancel?<button type="button" className="button button-ghost button-small" disabled={saving} onClick={onCancel}>{accepted?'关闭':'放弃本次选图'}</button>:null}
    </div>
    {notice?<p role="status" className="form-hint">{notice}</p>:null}
  </>;
  return retry?<section className="asset-picker" aria-label="重新选择此镜头商品图">{content}</section>:
    <details className="workspace-details" open={open} onToggle={event=>{
      if(dirty||saving){event.currentTarget.open=true;return;}
      setOpen(event.currentTarget.open);
      onOpenChange?.(event.currentTarget.open);
    }}><summary>产品图的制作引用（逐镜可选）</summary>{open?content:null}</details>;
}
