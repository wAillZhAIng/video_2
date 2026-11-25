import {useEffect,useRef,useState} from 'react';
import {Link} from 'react-router-dom';
import {request} from '../lib/api';
import type {ModelServicesOverview,RouteProfile,TaskCandidate} from '../lib/model-services-contract';
import {Badge,FormField} from './ui';
import {VideoServiceSelector} from './VideoServiceSelector';

const slots=[['character_image','人物、服装与场景图像'],['keyframe_image','分镜关键画面'],['video_upscale','视频超分']] as const;
type ModelSlot=typeof slots[number][0];
type State={busy:boolean;dirty:boolean;contextEnabled?:boolean;hasContext?:boolean;hasCloud?:boolean};
const choiceKey=(service:string,model:string)=>JSON.stringify([service,model]);
export function ProjectProductionModels({projectId,disabled,onStateChange,onSaved,eagerVideo=false,upscaleDisabled=disabled}:{projectId:string;disabled:boolean;upscaleDisabled?:boolean;onStateChange?:(state:State)=>void;onSaved?:()=>void|Promise<void>;eagerVideo?:boolean}){
  const [open,setOpen]=useState(false),[profiles,setProfiles]=useState<Partial<Record<ModelSlot,RouteProfile>>>({}),[overview,setOverview]=useState<ModelServicesOverview|null>(null),[drafts,setDrafts]=useState<Partial<Record<ModelSlot,string>>>({}),[error,setError]=useState(''),[loading,setLoading]=useState(false),[saving,setSaving]=useState(''),[reload,setReload]=useState(0);
  const [videoState,setVideoState]=useState<State>({busy:false,dirty:false});
  const callbacks=useRef({onStateChange,onSaved});callbacks.current={onStateChange,onSaved};
  const dirty=Object.keys(drafts).length>0;
  useEffect(()=>{callbacks.current.onStateChange?.({...videoState,busy:Boolean(saving)||videoState.busy,dirty:dirty||videoState.dirty});},[saving,dirty,videoState]);
  useEffect(()=>()=>callbacks.current.onStateChange?.({busy:false,dirty:false}),[]);
  useEffect(()=>{
    if(!open)return;
    const abort=new AbortController();
    const options={signal:abort.signal};
    setLoading(true);
    setError('');
    void request<ModelServicesOverview>('/api/model-services',options).then(async models=>{
      if(abort.signal.aborted)return;
      const routes=await Promise.all(slots.filter(([slot])=>slot!=='video_upscale'||models.slots.some(item=>item.slot===slot))
        .map(async([slot])=>{
          const profile=await request<RouteProfile>(`/api/model-services/routes/project/${encodeURIComponent(projectId)}/${slot}`,options);
          if(profile.target_id!==projectId||profile.scope!=='project'||profile.slot!==slot)throw new Error('模型配置响应不属于本项目和任务类型');
          return profile;
        }));
      if(abort.signal.aborted)return;
      setOverview(models);
      setProfiles(Object.fromEntries(routes.map(profile=>[profile.slot,profile])));
      setDrafts({});
    }).catch(e=>{if(!abort.signal.aborted)setError(String(e));}).finally(()=>{if(!abort.signal.aborted)setLoading(false);});
    return()=>abort.abort();
  },[projectId,open,reload]);

  async function save(slot:ModelSlot){
    const profile=profiles[slot],value=drafts[slot];
    if((slot==='video_upscale'?upscaleDisabled:disabled)||saving||!profile||value===undefined)return;
    setSaving(slot);
    setError('');
    try{
      const endpoint=`/api/model-services/routes/project/${encodeURIComponent(projectId)}/${slot}`;
      let updated:RouteProfile;
      if(value==='inherit'){
        updated=await request<RouteProfile>(endpoint,{method:'DELETE'});
      }else{
        const settings={...profile.settings,allow_fallback:false,allow_paid_fallback:false};
        if(slot==='video_upscale'&&value==='auto'){
          settings.mode='auto';
        }else{
          const [service_id,model_id]=JSON.parse(value) as string[];
          const selected=overview?.slots.find(item=>item.slot===slot)?.candidates?.find(item=>item.service_id===service_id&&item.model_id===model_id);
          if(!selected?.eligible)throw new Error('此模型尚未通过真实任务能力验证');
          settings.mode=slot==='video_upscale'?'manual':selected.is_cloud?'cloud_only':'local_only';
          settings.choices=[{service_id,model_id}];
        }
        updated=await request<RouteProfile>(endpoint,{method:'PUT',body:JSON.stringify({expected_revision:profile.revision,settings})});
      }
      setProfiles(previous=>({...previous,[slot]:updated}));
      setDrafts(previous=>{const next={...previous};delete next[slot];return next;});
      await callbacks.current.onSaved?.();
    }catch(e){setError(String(e));}
    finally{setSaving('');}
  }
  return <details className="workspace-details" open={open} onToggle={event=>setOpen(event.currentTarget.open)}><summary>本项目图像与视频模型（继承或项目级覆盖）</summary><p className="form-hint">下方读取当前项目的实际服务策略。仅有真实任务验证证据的候选可以选用；项目覆盖不会修改系统默认与其他项目。已接收的任务保留原模型快照。</p>{loading?<p role="status">读取项目模型与真实验证状态…</p>:null}{error?<p role="alert">{error}</p>:null}{slots.map(([slot,label])=>{const profile=profiles[slot];if(!profile)return null;const upscale=slot==='video_upscale',locked=upscale?upscaleDisabled:disabled;const candidates=overview?.slots.find(item=>item.slot===slot)?.candidates||[],saved=profile.settings.choices||[],value=drafts[slot]??(profile.inherited_from?'inherit':upscale&&profile.settings.mode==='auto'?'auto':saved.length===1?choiceKey(saved[0].service_id,saved[0].model_id||''):'current');const chosen=candidates.find(item=>choiceKey(item.service_id,item.model_id)===value);return <article className="project-model-setting" key={slot}><FormField label={label}><select value={value} disabled={locked||loading||Boolean(saving)} onChange={event=>setDrafts(previous=>({...previous,[slot]:event.target.value}))}><option value="inherit">继承系统策略{upscale&&profile.settings.mode==='auto'?' · 自动按画幅':''}</option>{upscale?<option value="auto">自动按分镜画幅选择</option>:null}{!profile.inherited_from&&saved.length!==1?<option value="current" disabled>当前项目策略 · {saved.length} 个已保存候选</option>:null}{saved.filter(choice=>!candidates.some(item=>item.service_id===choice.service_id&&item.model_id===(choice.model_id||''))).map(choice=><option key={choice.service_id} value={choiceKey(choice.service_id,choice.model_id||'')} disabled>{choice.service_id} · {choice.model_id||'默认模型'} · 尚未验证</option>)}{candidates.map(candidate=><option key={choiceKey(candidate.service_id,candidate.model_id)} value={choiceKey(candidate.service_id,candidate.model_id)} disabled={!candidate.eligible}>{candidate.is_cloud?'云端':'本地'} · {candidate.name} · {candidate.model_id}{upscale&&Array.isArray(candidate.evidence?.verified_aspect_ratios)?` · ${candidate.evidence.verified_aspect_ratios.join('、')}`:''}{candidate.eligible?'':' · 未验证'}</option>)}</select></FormField><p className="form-hint">来源：{profile.inherited_from||'项目独立设置'} · 修订 {profile.revision} · {profile.settings.mode||'按服务策略'}</p>{upscale&&profile.settings.mode==='auto'&&drafts[slot]===undefined?<p className="form-hint">按每镜实际画幅匹配已验证的 Real-ESRGAN 模型。</p>:drafts[slot]===undefined?saved.map(choice=>{const candidate=candidates.find(item=>item.service_id===choice.service_id&&(!choice.model_id||item.model_id===choice.model_id));return <p key={choice.service_id}><Badge tone={candidate?.eligible?'success':'warning'}>{candidate?.eligible?'真实任务能力已验证':'尚未验证可用性'}</Badge> {candidate?.name||choice.service_id} · {choice.model_id||candidate?.model_id||'默认模型'}{candidate?.reasons?.length?` · ${candidate.reasons.join('；')}`:''}</p>;}):chosen?<CandidateState candidate={chosen}/>:null}<div className="asset-actions"><button type="button" className="button button-secondary button-small" disabled={locked||loading||Boolean(saving)||drafts[slot]===undefined||value==='current'||value!=='inherit'&&value!=='auto'&&!chosen?.eligible} onClick={()=>void save(slot)}>{saving===slot?'正在保存…':`保存${label}模型`}</button>{drafts[slot]!==undefined?<button type="button" className="button button-ghost button-small" disabled={Boolean(saving)} onClick={()=>setDrafts(previous=>{const next={...previous};delete next[slot];return next;})}>放弃此模型选择</button>:null}</div></article>;})}{open||eagerVideo?<VideoServiceSelector key={projectId} projectId={projectId} disabled={disabled||Boolean(saving)} onStateChange={setVideoState} onSaved={()=>callbacks.current.onSaved?.()}/>:null}<div className="asset-actions"><button type="button" className="button button-secondary button-small" disabled={loading||dirty||Boolean(saving)} onClick={()=>setReload(value=>value+1)}>刷新实际模型</button><Link to="/settings">管理与验证服务档案</Link></div></details>;
}
function CandidateState({candidate}:{candidate:TaskCandidate}){return <p className="form-hint"><Badge tone={candidate.eligible?'success':'warning'}>{candidate.eligible?'真实任务能力已验证':'当前尚未验证'}</Badge> {candidate.reasons?.join('；')||'实际任务仍会检查模型连接和项目授权。'}</p>;}
