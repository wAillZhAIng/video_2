import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Link, useLocation } from 'react-router-dom';
import { ArrowRight, FileUp, RefreshCw, Plus, Play, Pause, FolderOpen, Wrench, Download, Sparkles, Clapperboard } from 'lucide-react';
import { FeishuWorkbenchPanel } from '../components/FeishuWorkbenchPanel';
import { ApiError, api, MODEL_CONNECTIONS_RECHECKED, request } from '../lib/api';
import type { PipelineStart, StudioOverview } from '../lib/studio-contract';
import { PageHeader, EmptyState, LoadingState, FormField } from '../components/ui';
import { useStudio, StudioProjectPicker, StudioStatus, recordText, shotTime, studioAssetPath, studioVideoDuration } from '../lib/studio-ui';
import { WORD_FILE_ACCEPT, WordImportPanel } from '../components/WordImportPanel';
import { ProjectProductPreparation } from '../components/ProjectProductPreparation';
import { ProjectProductReferences, type ProductReferenceState } from '../components/ProjectProductReferences';
import { ProjectProductionModels } from '../components/ProjectProductionModels';
import { ProjectAudioPolicy } from '../components/ProjectAudioPolicy';
import { ProjectReferenceVideos } from '../components/ProjectReferenceVideos';
import { ProjectVoicePreparation } from '../components/ProjectVoicePreparation';
import { ProjectCharacterReferences } from '../components/ProjectCharacterReferences';
import { QualitySettingsFields, QualityPlan, useQualityProfile } from '../components/QualitySettings';
import { SeedVrPreparation } from '../components/SeedVrPreparation';
import { ProjectShotFraming } from '../components/ProjectShotFraming';
import { AudioDurationResolution } from '../components/AudioDurationResolution';
import type { QualitySettings } from '../lib/quality-contract';
import '../studio.css';
import '../asset-library.css';

const nodeLabels: Record<string, string> = { character_reference: '人物参考素材', character: '人物参考素材', scene_reference: '场景参考', scene: '场景参考', keyframe: '镜头关键画面', video: '分镜视频', tts: '角色配音', tts_optional: '可选配音', audio_mix: '对白混音', lip_sync: '口型同步', quality: '画质处理', quality_check: '视频技术检查', parse: 'Word 解析' };
const repairStatusLabels:Record<string,string>={applied:'已执行',waiting:'等待',blocked:'受阻',skipped:'未执行',not_needed:'无需修复',unknown:'结果待核对',error:'诊断错误'};
const connectionStatusLabels: Record<string, string> = { connected: '已连接', recovered: '已恢复', busy: '运行中',
  reachable: '有响应，待核对配置', unavailable: '运行时不可用', unsupported: '缺少适配器', failed: '连接失败' };
const gpuReasonLabels:Record<string,string>={health_busy:'本地模型正占用GPU',health_idle:'本地模型空闲',runtime_port_refused:'默认剧本服务端口空闲',health_unreachable_and_no_managed_runtime_process:'健康检查超时，但进程登记确认本地模型未运行',health_unreachable_while_managed_runtime_process_is_alive:'健康检查超时且受管模型进程仍在运行',health_unreachable_and_runtime_process_state_unverified:'健康检查超时，模型进程状态无法确认',unexpected_runtime_identity_or_health_schema:'本地模型服务身份或健康数据异常'};
type VoicePreparationState = { ready:boolean;dirty:boolean;loading:boolean;busy:boolean;blockingReasons:string[] };
type StudioRepairReport = { run_status:string; gpu_gate:{state:'idle'|'busy'|'unknown';reason:string}; actions:{code:string;status:string;message:string;visual_id?:string;provider_job_id?:string|null;references?:{shot_id:string;reference_count:number;roles:Record<string,number>}[]}[]; blockers:{node_id?:string;kind?:string;status?:string;code?:string;message:string}[];
  model_connections?: { service_id: string; name: string; model_id: string; status: string; message: string }[] };
function isProductReferenceError(code:unknown,message:string){
  return code==='product_reference_identity_conflict'||(code==='comfyui_execution_failed'&&
    /商品参考身份不一致或无法确定|Model ImageCard did not establish one object subject for product-unit:/.test(message));
}
export function StudioPage() {
  const studio = useStudio();
  const { hash } = useLocation();
  const { data, projectId, projects, loading } = studio;
  const loadedProjectId=data?.project.id;
  useEffect(()=>{
    if(hash==='#project-output'&&loadedProjectId===projectId)document.getElementById('project-output')?.scrollIntoView?.();
  },[hash,loadedProjectId,projectId]);
  const quality=useQualityProfile(projectId?'project':'system',projectId||'default');
  const [file, setFile] = useState<File | null>(null), [cloudCosts, setCloudCosts] = useState(false),[paidFallback,setPaidFallback]=useState(false);
  const [feishuPanel, setFeishuPanel] = useState<'sync' | 'generate' | null>(null);
  const [fileInputKey,setFileInputKey] = useState(0);
  const [busy, setBusy] = useState(''), [error, setError] = useState(''), [notice, setNotice] = useState('');
  const [repairReport,setRepairReport]=useState<StudioRepairReport|null>(null);
  const [repairError,setRepairError]=useState('');
  const [newProject, setNewProject] = useState(false), [name, setName] = useState('');
  const [newRevision, setNewRevision] = useState(false);
  const [pendingImport, setPendingImport] = useState<{id:string;projectId:string;allowCosts:boolean;allowPaidFallback:boolean;quality:QualitySettings;qualityRevision:number;issues:string[]}|null>(null);
  const [importReviewBusy,setImportReviewBusy]=useState(false);
  const pendingImportRef=useRef(pendingImport);pendingImportRef.current=pendingImport;
  useEffect(()=>()=>{pendingImportRef.current=null;},[]);
  useEffect(()=>{
    if(pendingImport)document.getElementById('word-import-review')?.scrollIntoView?.({block:'start'});
  },[pendingImport]);
  const audioResolutionScope=`${projectId}:${data?.run?.id||'none'}`;
  const audioResolutionScopeRef=useRef(audioResolutionScope);audioResolutionScopeRef.current=audioResolutionScope;
  const [audioResolutionState,setAudioResolutionState]=useState<{scope:string;nodes:Record<string,boolean>}>({scope:'',nodes:{}});
  const audioResolutionBusy=audioResolutionState.scope===audioResolutionScope&&Object.values(audioResolutionState.nodes).some(Boolean);
  const onAudioResolutionBusy=useCallback((nodeId:string,value:boolean)=>{if(audioResolutionScopeRef.current===audioResolutionScope)setAudioResolutionState(previous=>({scope:audioResolutionScope,nodes:{...(previous.scope===audioResolutionScope?previous.nodes:{}),[nodeId]:value}}));},[audioResolutionScope]);
  const [productRepair,setProductRepair]=useState<{scope:string;nodeId:string}|null>(null);
  const [productRepairState,setProductRepairState]=useState<(ProductReferenceState&{scope:string})|null>(null);
  const repairNode=productRepair?.scope===audioResolutionScope?productRepair.nodeId:'';
  const repairState=productRepairState?.scope===audioResolutionScope?productRepairState:null;
  const pageBusy=Boolean(busy)||importReviewBusy||audioResolutionBusy||Boolean(repairState?.busy);
  const [voiceState,setVoiceState] = useState<VoicePreparationState & {projectId:string}>({projectId:'',ready:false,dirty:false,loading:true,busy:false,blockingReasons:[]});
  const onVoiceStateChange=useCallback((state:VoicePreparationState)=>setVoiceState({...state,projectId}),[projectId]);
  const voiceReady=voiceState.projectId===projectId&&voiceState.ready&&!voiceState.loading&&!voiceState.dirty&&!voiceState.busy;
  const [productReferenceState,setProductReferenceState]=useState({busy:false,dirty:false,overBudget:false});
  const onProductReferenceStateChange=useCallback((state:{busy:boolean;dirty:boolean;overBudget:boolean})=>setProductReferenceState(state),[]);
  const [modelState,setModelState]=useState<{busy:boolean;dirty:boolean;nativeH3Enabled?:boolean;hasNativeH3?:boolean;hasCloud?:boolean}>({busy:false,dirty:false});
  const qualityModeUse=!projectId||modelState.hasNativeH3||modelState.nativeH3Enabled?'generation':modelState.hasCloud?'cloud':'provider';
  const onModelStateChange=useCallback((state:{busy:boolean;dirty:boolean;nativeH3Enabled?:boolean;hasNativeH3?:boolean;hasCloud?:boolean})=>setModelState(state),[]);
  const framingScope=`${projectId}:${data?.run?.id||'none'}:${data?.run?.status||'none'}`;
  const framingScopeRef=useRef(framingScope);framingScopeRef.current=framingScope;
  const [framingState,setFramingState]=useState({scope:'',busy:false,dirty:false,requiresNewRun:false,ready:false});
  const framingReady=framingState.scope===framingScope&&framingState.ready;
  const onFramingStateChange=useCallback((state:{busy:boolean;dirty:boolean;requiresNewRun:boolean;ready:boolean})=>{if(framingScopeRef.current===framingScope)setFramingState({...state,scope:framingScope});},[framingScope]);
  const [audioPolicyRevision,setAudioPolicyRevision]=useState(0),[audioPolicyState,setAudioPolicyState]=useState({busy:false,dirty:false});
  const onAudioPolicyStateChange=useCallback((state:{busy:boolean;dirty:boolean})=>setAudioPolicyState(state),[]);
  const [referenceState,setReferenceState]=useState({busy:false,dirty:false});
  const onReferenceStateChange=useCallback((state:{busy:boolean;dirty:boolean})=>setReferenceState(state),[]);
  const [referenceVideoState,setReferenceVideoState]=useState({busy:false,dirty:false});
  const voiceUnsettled=(voiceState.projectId===projectId&&(voiceState.busy||voiceState.dirty))||referenceState.busy||referenceState.dirty;
  const preparationUnsettled=voiceUnsettled||referenceVideoState.busy||referenceVideoState.dirty||audioPolicyState.busy||audioPolicyState.dirty||modelState.busy||modelState.dirty||productReferenceState.busy||productReferenceState.dirty||framingState.busy||framingState.dirty||Boolean(repairState?.dirty);
  const activeRun=Boolean(data?.run&&!['completed','cancelled','failed','completed_with_errors','waiting_review'].includes(data.run.status));
  const nodes = Array.isArray(data?.run?.nodes_json) ? data.run.nodes_json as Record<string, unknown>[] : [];
  const completedNodes=new Set(nodes.filter(node=>node.status==='succeeded').map(node=>node.id));
  const stalledProductRun=data?.run?.status==='running'&&
    nodes.some(node=>node.kind==='video'&&node.status==='exception'&&isProductReferenceError(node.code,recordText(node.error)))&&
    !nodes.some(node=>['dispatching','running','retrying','unknown'].includes(recordText(node.status))||
      ['pending','waiting_dependencies'].includes(recordText(node.status))&&
      (!Array.isArray(node.dependencies)||node.dependencies.every(dependency=>completedNodes.has(dependency))));
  const productRunLocked=activeRun&&!['paused','exceptions'].includes(data?.run?.status||'');
  const productInputsChanged=Boolean(data?.run?.snapshot_json?.product_references_changed);
  const [focusPreparation,setFocusPreparation]=useState('');
  useEffect(()=>{if(projectId&&focusPreparation===projectId&&data?.project.id===projectId){const target=document.getElementById('project-voice-preparation');if(target){target.scrollIntoView?.({behavior:'smooth',block:'start'});setFocusPreparation('');}}},[projectId,focusPreparation,data?.project.id]);
  useEffect(()=>{if(!importReviewBusy)return;const guard=(event:MouseEvent)=>{if(event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;const target=event.target instanceof Element?event.target.closest('a[href]'):null;if(target){event.preventDefault();event.stopPropagation();setNotice('正在确认并保存剧本，请稍候再切换页面。');}};document.addEventListener('click',guard,true);return()=>document.removeEventListener('click',guard,true);},[importReviewBusy]);
  useEffect(()=>{
    const busy=referenceState.busy||referenceVideoState.busy||modelState.busy||audioPolicyState.busy||productReferenceState.busy||framingState.busy||audioResolutionBusy||repairState?.busy,dirty=referenceState.dirty||referenceVideoState.dirty||modelState.dirty||audioPolicyState.dirty||productReferenceState.dirty||framingState.dirty||repairState?.dirty;
    if((!busy&&!dirty)||voiceState.busy||voiceState.dirty)return;
    const unload=(event:BeforeUnloadEvent)=>{event.preventDefault();event.returnValue='';};
    const navigate=(event:MouseEvent)=>{if(event.defaultPrevented||event.button!==0||event.ctrlKey||event.metaKey||event.shiftKey||event.altKey)return;const link=event.target instanceof Element?event.target.closest<HTMLAnchorElement>('a[href]'):null;if(!link||link.hasAttribute('download')||(link.target&&link.target!=='_self'))return;const next=new URL(link.href,window.location.href),here=new URL(window.location.href);if(!['http:','https:'].includes(next.protocol)||next.origin===here.origin&&next.pathname===here.pathname&&next.search===here.search)return;if(busy){event.preventDefault();event.stopPropagation();setNotice('制作设置或人物参考图正在保存，请完成后再离开。');}else if(!window.confirm('制作设置或人物参考图有未保存的选择。确定放弃这些草稿并离开吗？')){event.preventDefault();event.stopPropagation();setNotice('已保留未保存的制作准备草稿。');}};
    document.addEventListener('click',navigate,true);window.addEventListener('beforeunload',unload);return()=>{document.removeEventListener('click',navigate,true);window.removeEventListener('beforeunload',unload);};
  },[referenceState,referenceVideoState,modelState,audioPolicyState,productReferenceState,framingState,voiceState.busy,voiceState.dirty,audioResolutionBusy,repairState]);
  async function perform(label: string, work: () => Promise<unknown>) {
    setBusy(label); setError(''); setNotice('');
    try { await work(); await studio.refreshList(); await studio.refreshAfterChange(); }
    catch (e) { setError(String(e)); }
    finally { setBusy(''); }
  }
  async function importWord(e: FormEvent) {
    e.preventDefault(); if (pageBusy||pendingImport||!file||!quality.profile||quality.loading||!quality.supported) return;
    const autoStart = (e.nativeEvent as SubmitEvent).submitter?.getAttribute('value') === 'automatic';
    await perform('import', async () => {
      const form = new FormData(); form.set('file', file); form.set('auto_start', String(autoStart));
      form.set('allow_cloud_costs', String(cloudCosts)); form.set('allow_paid_fallback',String(paidFallback)); if (projectId) form.set('project_id', projectId);
      form.set('new_revision', String(Boolean(projectId)&&newRevision));
      form.set('quality_json',JSON.stringify(quality.settings));
      const result = await request<{ project_id?: string; pipeline?: {id: string}; status: string; import_id:string; issues?:string[] }>('/api/studio/import', { method: 'POST', body: form });
      setFile(null);setFileInputKey(value=>value+1);
      if(result.status==='pending_review') {
        setPendingImport({id:result.import_id,projectId:projectId,allowCosts:cloudCosts,allowPaidFallback:paidFallback,quality:{...quality.settings},qualityRevision:projectId?quality.profile!.revision:0,issues:result.issues||[]});
        setNotice('文档中有需要核对的身份或解析歧义。请在下方修正并确认，再配置角色配音和制作画质。');
      } else {
        setPendingImport(null);
        if(result.project_id){studio.selectProject(result.project_id);if(!result.pipeline)setFocusPreparation(result.project_id);}
        setNotice(result.pipeline ? 'Word 已导入，自动制作已提交。可在工作台查看实际进度和成片。'
          : 'Word 已导入。可补充参考图或配音，再点击「开始／增量续作」自动成片。');
      }
    });
  }
  const runRenderId = data?.run?.result_json?.render_id;
  const completedExports = data?.renders?.filter(render => render.kind === 'render'
    && render.status === 'generated' && render.relative_path) || [];
  const finalRender = data?.renders?.find(render => render.id === runRenderId)
    || (!runRenderId ? [...completedExports].sort((a, b) =>
      String(b.created_at || '').localeCompare(String(a.created_at || '')))[0] : undefined);
  const finalPlayback = finalRender?.status === 'generated' && finalRender.relative_path ? api.mediaUrl(finalRender.relative_path) : '';
  const finalInfo = finalRender?.metadata_json?.actual;
  const previousExports = data?.renders?.filter(render => render.id !== finalRender?.id && render.kind === 'render'
    && render.status === 'generated' && render.relative_path) || [];
  const autoFinal = data?.run?.result_json?.automatic_final;
  const retryFinal = data?.run?.status === 'completed_with_errors' && ['blocked','failed','cancelled'].includes(autoFinal?.status);
  const importDisabled = pageBusy||preparationUnsettled||Boolean(pendingImport)||!file||quality.loading||quality.saving||!quality.profile||!quality.supported;
  const outputLink = `/?project=${encodeURIComponent(projectId)}#project-output`;
  async function control(action: 'start' | 'pause' | 'resume' | 'retry', nodeId?: string) {
    await perform(action, async () => {
      if((action==='start'||action==='resume')&&!framingReady)throw new Error('请先重新读取实际镜头画幅，确认镜头与编排状态后再开始或继续制作。');
      if(action==='start'&&(!quality.profile||!quality.supported))throw new Error('请先读取并核对制作画质配置。');
      if(action==='start'){
        if(modelState.hasCloud&&!cloudCosts)throw new Error('当前视频服务包含云端模型，请先确认云端调用和供应商费用。');
        if(productReferenceState.overBudget)throw new Error('此镜头产品参考超过模型预算，请在制作设置中明确减少对应图片。');
        if(productReferenceState.busy||productReferenceState.dirty)throw new Error('请先保存或放弃产品制作参考选择。');
        if(modelState.busy||modelState.dirty)throw new Error('请先保存或放弃项目模型选择。');
        if(framingState.busy||framingState.dirty)throw new Error('请先保存或重新读取实际镜头画幅，再开始制作。');
        if(audioPolicyState.busy||audioPolicyState.dirty)throw new Error('请先保存项目声音方式。');
        if(referenceState.busy||referenceState.dirty)throw new Error('请先保存或放弃角色参考图草稿。');
        if(!voiceReady)throw new Error(voiceState.dirty?'请先保存角色配音设置。':voiceState.blockingReasons.join('；')||'请先完成角色配音准备检查。');
        const voices=await request<{ready_to_start:boolean;blockers:string[]}>(`/api/studio/projects/${projectId}/voices?allow_cloud_costs=${cloudCosts}&allow_paid_fallback=${paidFallback}`);
        if(!voices.ready_to_start)throw new Error(voices.blockers.join('；')||'角色配音条件未完成，请先配置。');
      }
      if(action==='start'&&quality.dirty)await quality.save();
      const payload: PipelineStart | { node_id?: string } = action === 'start' ? { allow_cloud_costs: cloudCosts,allow_paid_fallback:paidFallback,quality:quality.settings } : nodeId ? { node_id: nodeId } : {};
      await request<StudioOverview>(`/api/studio/projects/${projectId}/${action}`, { method: 'POST', body: JSON.stringify(payload) });
      setNotice(action === 'pause' ? '已请求暂停后续任务；正在运行的模型请求以服务实际状态为准。' : '请求已提交，执行结果将在任务列表更新。');
    });
  }
  async function applyQuality(){
    if(!projectId||!data?.run||pageBusy||quality.loading||quality.saving||!quality.supported)return;
    await perform('quality-apply',async()=>{
      const saved=await quality.save();
      const result=await request<{applied:string[];skipped:{shot_id:string;reason:string}[];run_status:string}>(`/api/studio/projects/${encodeURIComponent(projectId)}/quality/apply`,{method:'POST',body:JSON.stringify({expected_profile_revision:saved.revision})});
      const skipped=Array.isArray(result.skipped)?result.skipped:[];
      setNotice(`已将保存的画质应用到 ${result.applied.length} 个未派发镜头。已接收任务与原视频保留。${skipped.length?`其余 ${skipped.length} 镜：${[...new Set(skipped.map(item=>item.reason))].join('；')}`:''}`);
    });
  }
  async function oneClickRepair() {
    if(!projectId)return;
    setRepairReport(null);
    setRepairError('');setError('');setNotice('');setBusy('repair');
    try {
      const report=await request<StudioRepairReport>(`/api/studio/projects/${projectId}/repair`,{method:'POST',body:'{}'});
      setRepairReport(report);
      const repaired = report.actions.some(action => action.status === 'applied')
        || report.model_connections?.some(service => service.status === 'recovered');
      setNotice(repaired ? '已完成恢复。模型连接与任务状态见下方。' : '检查已完成。模型连接与任务状态见下方。');
      window.dispatchEvent(new Event(MODEL_CONNECTIONS_RECHECKED));
      await studio.refreshList();
      await studio.refreshAfterChange();
    } catch(e) {
      if (e instanceof ApiError && e.code === 'studio_product_inputs_changed') {
        const partial = (e.details as { repair_report?: StudioRepairReport } | undefined)?.repair_report;
        if (partial) {
          setRepairReport(partial);
          window.dispatchEvent(new Event(MODEL_CONNECTIONS_RECHECKED));
        }
        setRepairError(e.message);
        return;
      }
      setRepairError(e instanceof ApiError && e.status===404
        ? '当前本地后端尚未加载一键修复接口（HTTP 404）。请更新并重启后端后重试。'
        : `一键修复请求失败：${e instanceof Error ? e.message : String(e)}`);
    } finally {
      setBusy('');
    }
  }
  const shots = data?.shots || [], characters = data?.characters || [], scenes = data?.scenes || [];
  const videoCount = shots.filter(shot => shot.video?.relative_path).length;
  const finishedNodes = nodes.filter(node => ['succeeded', 'completed', 'reused', 'skipped'].includes(recordText(node.status))).length;
  const connections = repairReport?.model_connections || [];
  const connectedCount = connections.filter(service => ['connected', 'recovered', 'busy'].includes(service.status)).length;
  return <div className="studio-workspace">
    <PageHeader title="项目工作台" description="导入剧本后自动生成分镜和成片。需要绑定人物、产品或声音时，可在制作开始前配置。" actions={<div className="toolbar"><button type="button" className="button button-secondary" onClick={() => setFeishuPanel(v => v === 'sync' ? null : 'sync')}><Sparkles size={16}/>一键同步飞书</button><button type="button" className="button button-primary" onClick={() => setFeishuPanel(v => v === 'generate' ? null : 'generate')}><Clapperboard size={16}/>一键生成视频</button><button type="button" className="button button-secondary" disabled={preparationUnsettled||pageBusy||Boolean(pendingImport)} onClick={() => setNewProject(value => !value)}><Plus size={16}/>新建项目</button></div>}/>
    {feishuPanel ? <FeishuWorkbenchPanel mode={feishuPanel} onClose={() => setFeishuPanel(null)} onProjectsChanged={() => { void studio.refreshList(); }} /> : null}
    <div className="studio-toolbar"><StudioProjectPicker projects={projects} projectId={projectId} selectedName={data?.project.name} disabled={pageBusy||preparationUnsettled||Boolean(pendingImport)||quality.saving||quality.loading} onChange={studio.selectProject}/><button className="button button-secondary" disabled={pageBusy||preparationUnsettled} onClick={() => { void studio.refreshList(); void studio.refresh(); }}><RefreshCw size={15}/>刷新</button>{projectId ? <a className="button button-primary" href={outputLink}>查看成片<ArrowRight size={16}/></a> : null}</div>
    {newProject ? <form className="card studio-panel studio-new-project" onSubmit={e => { e.preventDefault(); if (!name.trim()) return; void perform('create', async () => { const project = await api.createProject({ name: name.trim() }); studio.selectProject(project.id); setName(''); setNewProject(false); }); }}>
      <FormField label="项目名称" required><input value={name} maxLength={200} onChange={e => setName(e.target.value)} required/></FormField><button className="button button-primary" disabled={pageBusy||!name.trim()}>创建项目</button><button type="button" className="button button-secondary" onClick={() => setNewProject(false)}>取消</button>
    </form> : null}
    <section className="card studio-import studio-preparation-step" aria-label="第一步 导入 Word，准备制作"><p className="studio-step-label">第一步</p>
      <div className="studio-import-copy"><FileUp size={28} aria-hidden="true"/><div><h2>导入 Word，准备制作</h2><p>{projectId ? `导入至「${data?.project.name || projects.find(p => p.id === projectId)?.name || '当前项目'}」，相同文档按已有记录复用。` : '未选择项目时，根据剧本自动创建项目。'} 导入后先配置角色参考音频，再确认开始制作。</p></div></div>
      {data?.script?<div className="inline-notice" role="status"><strong>已加载保存的剧本</strong><p>{recordText(data.project.source_filename||data.script.filename||data.script.source_filename||data.script.title)||'已保存的结构化剧本和源文档'} · {scenes.length} 个场景 · {shots.length} 个分镜 · {characters.length} 位角色 · 解析状态：{recordText(data.script.status)||'已保存'}</p><p>已识别角色：{characters.map(character=>recordText(character.display_name||character.name)).join('、')||'暂无角色'}</p><p>已识别场景：{scenes.map(scene=>recordText(scene.name||scene.title)).join('、')||'暂无场景'}</p><p>此项目可直接继续制作，无需重新导入同一份 Word。下面仅用于导入新的文档或修订。</p></div>:null}
      <form onSubmit={importWord}>
        <FormField label="Word 剧本文档" hint="支持 DOCX、DOCM、DOTX、DOTM、DOC、DOT 和 RTF。"><input key={fileInputKey} type="file" accept={WORD_FILE_ACCEPT} onChange={e => setFile(e.target.files?.[0] || null)} disabled={pageBusy||preparationUnsettled||Boolean(pendingImport)}/></FormField>
        {projectId?<label className="studio-check"><input type="checkbox" checked={newRevision} onChange={e=>setNewRevision(e.target.checked)}/><span>将改动的 Word 保存为本项目新剧本修订。相同文档会复用已有结果，原人物身份、素材和历史视频保留。</span></label>:null}
        <label className="studio-check"><input type="checkbox" checked={cloudCosts} onChange={e => {setCloudCosts(e.target.checked);if(!e.target.checked)setPaidFallback(false);}} disabled={pageBusy||preparationUnsettled}/><span>允许本项目调用已配置的云端生成服务，并承担供应商实际费用。未勾选时仅执行获准的本地任务。</span></label>
        <label className="studio-check"><input type="checkbox" checked={paidFallback} onChange={e=>setPaidFallback(e.target.checked)} disabled={pageBusy||preparationUnsettled||!cloudCosts}/><span>允许本次制作在已保存策略许可时回退到付费云端；仅开启云端调用不会自动授权付费回退。</span></label>
        <div className="studio-import-bottom"><div className="studio-actions"><button type="submit" value="automatic" className="button button-primary" disabled={importDisabled}><FileUp size={16}/>{busy === 'import' ? '正在导入…' : '导入并自动成片'}</button><button type="submit" value="prepare" className="button button-secondary" disabled={importDisabled}>仅导入，先配置素材</button></div></div>
      </form>
    </section>
    {pendingImport?<div id="word-import-review">
      <p className="inline-notice" role="status">{notice || 'Word 已保存，正在等待审核。'}</p>
      <WordImportPanel key={pendingImport.id} importId={pendingImport.id} studioMode initialProjectId={pendingImport.projectId} onBusyChange={setImportReviewBusy} onClose={()=>{setPendingImport(null);setNotice('');}} onImported={async id=>{
        const pending=pendingImport;
        if(pendingImportRef.current?.id!==pending.id)return;
        try{await request('/api/quality/profiles/project/'+id,{method:'PUT',body:JSON.stringify({settings:pending.quality,expected_revision:pending.qualityRevision})});}
        catch(e){if(pendingImportRef.current?.id===pending.id)setError('剧本已导入，但画质配置未保存，请核对当前项目制作设置：'+String(e));}
        if(pendingImportRef.current?.id!==pending.id)return;
        if(id===projectId)await quality.load();
        if(pendingImportRef.current?.id!==pending.id)return;
        await studio.refreshList();if(id===projectId)await studio.refreshAfterChange();if(pendingImportRef.current?.id!==pending.id)return;studio.selectProject(id);setFocusPreparation(id);setPendingImport(null);setNotice('剧本已确认，尚未启动制作。请先配置角色配音并核对画质，再开始制作。');
      }}/>
    </div>:null}
    <section className="card studio-panel studio-preparation-step" aria-label="第二步 制作设置"><p className="studio-step-label">第二步</p><h2>制作设置</h2><p>选择本次画质，导入后核对角色配音，再开始制作。{projectId?'未覆盖的镜头继承项目设置；已接收任务和旧视频保留原快照。':'新项目使用下方设置，不会更改系统默认。'}</p>
      {projectId?<ProjectProductionModels eagerVideo key={`models:${projectId}`} projectId={projectId} upscaleDisabled={pageBusy||Boolean(pendingImport)} disabled={pageBusy||voiceUnsettled||Boolean(pendingImport)||(activeRun&&data?.run?.status!=='paused')} onStateChange={onModelStateChange} onSaved={studio.refreshAfterChange}/>:null}
      {projectId?<ProjectShotFraming key={`framing:${projectId}:${data?.run?.id||'none'}:${data?.run?.status||'none'}`} projectId={projectId} disabled={pageBusy||Boolean(pendingImport)||quality.saving} onStateChange={onFramingStateChange} onSaved={(ratio,uniform)=>{if(uniform)quality.setSettings(previous=>({...previous,aspect_ratio:ratio}));}}/>:null}
      {projectId&&data?.project.id===projectId?<ProjectReferenceVideos key={`reference-videos:${projectId}:${data?.script?.id||'none'}`} projectId={projectId} shots={shots} disabled={pageBusy||Boolean(pendingImport)||(activeRun&&data.run?.status!=='paused')} onStateChange={setReferenceVideoState} onSaved={studio.refreshAfterChange}/>:null}
      {projectId?<ProjectAudioPolicy key={`audio-policy:${projectId}`} projectId={projectId} onStateChange={onAudioPolicyStateChange} disabled={pageBusy||voiceUnsettled||Boolean(pendingImport)||(activeRun&&data?.run?.status!=='paused')} onSaved={async()=>{setAudioPolicyRevision(value=>value+1);await studio.refreshAfterChange();}}/>:null}
      {quality.loading?<p role="status">读取制作画质设置…</p>:null}{quality.error?<p role="alert" className="inline-notice warning">{quality.error}<button type="button" className="button button-secondary button-small" onClick={()=>void quality.load()}>重新读取画质</button></p>:null}
      {quality.profile?<><p>来源：{quality.profile.inherited_from||'项目独立配置'} · 修订 {quality.profile.revision}</p>{modelState.hasCloud?<p role="status" className="inline-notice">当前策略包含云端候选；启用云端调用时将使用供应商已支持的参数。</p>:null}<QualitySettingsFields modeUse={qualityModeUse} value={quality.settings} onChange={quality.setSettings} capabilities={quality.capabilities} capabilityError={quality.capabilityError} onRefreshCapabilities={quality.refreshCapabilities} disabled={pageBusy||quality.saving||quality.loading}/>{projectId?<button type="button" className="button button-secondary" disabled={pageBusy||quality.saving||quality.loading||!quality.supported} onClick={()=>void quality.save().then(()=>setNotice('项目制作画质已保存，后续未覆盖镜头继承此配置。')).catch(()=>{})}>保存项目制作设置</button>:null}{projectId&&data?.run?<button type="button" className="button button-primary" disabled={pageBusy||quality.saving||quality.loading||!quality.supported} onClick={()=>void applyQuality()}>保存并应用到未派发画质任务</button>:null}{projectId?<SeedVrPreparation key={`seedvr:${projectId}`} projectId={projectId} settings={quality.settings} capabilities={quality.capabilities} shots={shots.map(shot=>({id:shot.id,order:shot.order,title:shot.title}))} disabled={pageBusy||quality.saving||quality.loading} onVerified={quality.refreshCapabilities}/>:null}{quality.dirty?<p className="studio-muted">本次选择尚未保存；上传时会随文档原子传入，开始续作时先保存。下方显示已保存方案。</p>:null}<QualityPlan plan={quality.profile.plan} modeUse={qualityModeUse}/></>:null}
    </section>
    <section className="card studio-panel studio-preparation-step studio-role-settings" aria-label="第三步 角色设置"><p className="studio-step-label">第三步</p><h2>角色设置</h2><p>为每个角色保存声音与可选的人物参考图。绑定跟随角色 ID，未上传参考图时继续使用剧本描述和已有身份。</p>
    {projectId&&data?.project.id===projectId?<div id="project-voice-preparation"><ProjectVoicePreparation projectId={projectId} refreshKey={`${data?.script?.id||''}:${audioPolicyRevision}`} allowCloudCosts={cloudCosts} allowPaidFallback={paidFallback} disabled={pageBusy||Boolean(pendingImport)||quality.saving||(activeRun&&data.run?.status!=='paused')} onStateChange={onVoiceStateChange} onChanged={studio.refreshAfterChange}/></div>:null}
    {projectId&&data?.project.id===projectId?<ProjectCharacterReferences key={projectId} projectId={projectId} refreshKey={data.script?.id||''} disabled={pageBusy||Boolean(pendingImport)||(activeRun&&data.run?.status!=='paused')} onStateChange={onReferenceStateChange} onChanged={studio.refreshAfterChange}/>:<p className="form-hint">先导入剧本建立角色，再配置声音与人物参考图。</p>}
    {projectId&&data?.project.id===projectId?<ProjectProductPreparation key={`products:${projectId}`} projectId={projectId}
      shots={shots.map(shot=>({id:shot.id,order:shot.order,title:shot.title}))} bulkDisabled={productRunLocked}
      disabled={pageBusy||Boolean(pendingImport)||Boolean(repairState?.dirty)||(productRunLocked&&!stalledProductRun)}
      onStateChange={onProductReferenceStateChange} onChanged={studio.refreshAfterChange}/>:null}
    </section>
    {(error || studio.error) ? <div role="alert" className="inline-notice warning">{error || studio.error}<Link to="/settings">检查系统设置</Link></div> : null}
    {notice && !pendingImport ? <p className="inline-notice" role="status">{notice}</p> : null}
    {loading ? <LoadingState label="读取项目与真实制作记录…"/> : null}
    {!projectId ? <section className="studio-projects"><h2>已有项目</h2>{projects.length ? <div className="studio-project-grid">{projects.map(project => <button key={project.id} type="button" className="card studio-project-card" onClick={() => studio.selectProject(project.id)}><FolderOpen size={22}/><strong>{project.name}</strong><StudioStatus status={project.status}/><span>{project.shot_count || 0} 镜 · {project.video_count || 0} 条视频{project.error_count ? ` · ${project.error_count} 项异常` : ''}</span></button>)}</div> : studio.listLoading ? <LoadingState label="读取已有项目…"/> : studio.error ? <p className="studio-muted">项目列表暂未读取成功。恢复连接后刷新即可继续查看已有记录。</p> : <EmptyState title="从第一份剧本开始" description="上传 Word 即可创建项目；已有项目会显示在这里。"/>}</section> : null}
    {data ? <>
      <section className="studio-metrics" aria-label="制作概览"><div><strong>{characters.length}</strong><span>已关联人物</span></div><div><strong>{scenes.length}</strong><span>结构化场景</span></div><div><strong>{shots.length}</strong><span>真实分镜记录</span></div><div><strong>{videoCount} / {shots.length}</strong><span>已有视频文件</span></div></section>
      {modelState.hasCloud&&!cloudCosts?<p role="status" className="inline-notice">当前视频流程包含云端模型。请先勾选本项目的云端调用与费用授权，再开始制作。</p>:null}
      <section className="card studio-panel"><div className="studio-section-heading"><div><h2>自动制作状态</h2><StudioStatus status={data.run?.status}/></div><div className="studio-actions">
        {!data.run || ['completed','cancelled','failed','completed_with_errors','waiting_review'].includes(data.run.status) || (productInputsChanged||framingState.requiresNewRun)&&['paused','exceptions'].includes(data.run.status) ? <button className="button button-primary" disabled={pageBusy||preparationUnsettled||productReferenceState.overBudget||!voiceReady||!shots.length||quality.loading||quality.saving||!quality.profile||!quality.supported||!framingReady||Boolean(modelState.hasCloud&&!cloudCosts)} onClick={() => void control('start')}><Play size={15}/>开始／增量续作</button> : null}
        {data.run?.status === 'paused'&&!productInputsChanged&&!framingState.requiresNewRun ? <button className="button button-primary" disabled={pageBusy||preparationUnsettled||!framingReady||Boolean(modelState.hasCloud&&!cloudCosts)} onClick={() => void control('resume')}><Play size={15}/>继续制作</button> : null}
        {data.run && !['completed','cancelled','paused','failed','completed_with_errors','rendering'].includes(data.run.status) ? <button className="button button-secondary" disabled={pageBusy} onClick={() => void control('pause')}><Pause size={15}/>暂停后续任务</button> : null}
        {data.run ? <button type="button" className="button button-secondary" disabled={pageBusy||Boolean(repairState?.dirty)} onClick={() => void oneClickRepair()}><Wrench size={15}/>{busy==='repair'?'正在检查并恢复模型…':'一键修复'}</button> : null}
        </div></div>
        {repairError ? <p className="inline-notice warning" role="alert" aria-live="assertive">{repairError}</p> : null}
        {productInputsChanged?<p className="inline-notice">产品参考已更改。点击「开始／增量续作」使用新参考重新核对受影响镜头；已有视频保留。</p>:null}
        {repairReport ? <div className="inline-notice studio-repair-report" role="status" aria-live="polite">
          <strong>一键修复诊断结果</strong>
          {connections.length > 0 && <details open>
            <summary>模型连接 {connectedCount} / {connections.length}</summary>
            <ul>{connections.map(service => <li key={service.service_id}>
              <strong>{service.name}</strong>{service.model_id ? ` · ${service.model_id}` : ''}
              {' · '}{connectionStatusLabels[service.status] || service.status}。{service.message}
            </li>)}</ul>
          </details>}
          <p>任务状态：{repairReport.run_status} · GPU门控：{repairReport.gpu_gate.state==='idle'?'空闲':repairReport.gpu_gate.state==='busy'?'占用':'状态未知'}（{gpuReasonLabels[repairReport.gpu_gate.reason]||repairReport.gpu_gate.reason}）</p>
          {repairReport.actions.map((action,index)=><div key={`${action.code}:${index}`}><p>{repairStatusLabels[action.status]||action.status} · {action.message}{action.provider_job_id?` · 服务任务 ${action.provider_job_id}`:''}</p>{action.references?.map(reference=><p key={reference.shot_id}>第 {shots.find(shot=>shot.id===reference.shot_id)?.order||'?'} 镜：{reference.reference_count} 张原图已完整编译（人物 {reference.roles.identity||0}、场景 {reference.roles.scene||0}、产品 {reference.roles.product||0}），未删除参考图。生成进度以对应关键画面节点为准。</p>)}</div>)}
          {repairReport.blockers.map((blocker,index)=><p key={`${blocker.node_id||blocker.code}:${index}`}>仍需处理：{nodeLabels[blocker.kind||'']||blocker.kind||'任务'} · {blocker.code||blocker.status} · {blocker.message}</p>)}
        </div> : null}
        {!voiceReady&&!activeRun?<p className="studio-muted">{voiceState.dirty?'角色配音有未保存设置，请先保存。':voiceState.blockingReasons.join('；')||'正在检查制作前的角色配音配置…'}</p>:null}
        {data.run?.error_message ? <p className="inline-notice warning">{recordText(data.run.error_message)}</p> : null}
        {nodes.length ? <><p className="studio-muted">已结束 {finishedNodes} / {nodes.length} 个任务。以下为后端实际状态，等待参考素材的镜头会自动接续。</p><div className="studio-node-list">{nodes.map((node, index) => <div key={recordText(node.id)||index} className="studio-node"><strong>{nodeLabels[recordText(node.kind)] || recordText(node.kind)}<small>{recordText(node.target_name) || recordText(node.target_id)}</small></strong><StudioStatus status={recordText(node.status)}/><span>{recordText(node.error)}</span>{Number(node.attempts) > 0 ? <small>已尝试 {recordText(node.attempts)} 次</small> : null}</div>)}</div></> : <p className="studio-muted">暂无制作任务。上传 Word 或开始增量续作后，会在这里显示真实依赖与执行结果。</p>}
      </section>
      {Boolean(data.exceptions?.length) ? <section className="card studio-panel studio-exceptions"><h2>需要处理的异常</h2><p>任务失败或条件缺失会保留真实错误。结果不明的服务请求须先核对，避免重复提交。</p>{data.exceptions?.map((item, index) => {
        const audioTooLong=Boolean(item.node_id)&&(item.code==='audio_longer_than_video'||recordText(item.message||item.error).includes('对白音频超过原片长度'));
        const nodeId=recordText(item.node_id),runId=recordText(data.run?.id),message=recordText(item.message)||recordText(item.error);
        const targetId=recordText(item.target_id);
        const frozenRun=data.run?.snapshot_json;
        const frozenOrder=Array.isArray(frozenRun?.shot_ids)?frozenRun.shot_ids.indexOf(targetId)+1:0;
        const shot=shots.find(shot=>shot.id===targetId);
        const retryable=['failed','blocked','waiting_user','waiting_exception','exception'].includes(item.status);
        const productMismatch=Boolean(runId&&nodeId&&shot&&retryable&&item.kind==='video'
          &&isProductReferenceError(item.code,message));
        const repairing=productMismatch&&repairNode===nodeId;
        return <article key={item.node_id || index}>
          <div><strong>{message}</strong><small>{recordText(item.code)}{item.target_id ? ` · ${recordText(item.target_id)}` : ''}</small></div>
          {productMismatch?<p className="form-hint">本镜产品图未通过商品主体识别。请选择清楚显示同一商品的照片，或清空本镜的视频产品图。</p>:null}
          {productMismatch?<button type="button" className="button button-secondary button-small" aria-expanded={repairing}
            disabled={pageBusy||repairing||productReferenceState.dirty||productReferenceState.busy||Boolean(repairState?.dirty)}
            onClick={()=>setProductRepair({scope:audioResolutionScope,nodeId})}>重新选择此镜头商品图</button>:
            !audioTooLong&&nodeId&&retryable?<button type="button" className="button button-secondary button-small" disabled={pageBusy||Boolean(repairState?.dirty)} onClick={()=>void control('retry',nodeId)}>重试此任务</button>:null}
          {repairing&&shot?<ProjectProductReferences key={`${audioResolutionScope}:${nodeId}`} projectId={projectId} shots={[shot]}
            retry={{runId,nodeId,shotId:shot.id}} disabled={pageBusy}
            onStateChange={state=>{if(audioResolutionScopeRef.current===audioResolutionScope)setProductRepairState({...state,scope:audioResolutionScope});}}
            onCancel={()=>setProductRepair(null)}
            onChanged={async()=>{
              if(audioResolutionScopeRef.current!==audioResolutionScope)return;
              setNotice(`${shot.order>0?`第 ${shot.order} 镜`:'此分镜'}已保存新的视频商品图并提交重试。`);
              await studio.refreshAfterChange();
            }}/>:null}
          {audioTooLong?<AudioDurationResolution key={`${audioResolutionScope}:${item.node_id}`} projectId={projectId} nodeId={String(item.node_id)} disabled={pageBusy} onBusyChange={value=>onAudioResolutionBusy(String(item.node_id),value)} onApplied={async()=>{if(audioResolutionScopeRef.current===audioResolutionScope)await studio.refreshAfterChange();}}/>:
            !productMismatch?<Link to={item.kind?.includes('character') ? `/characters/${item.target_id}?project=${encodeURIComponent(projectId)}` : `/storyboard?project=${encodeURIComponent(projectId)}`}>查看并处理</Link>:null}
        </article>;
      })}</section> : null}
      <div className="studio-two-column"><section className="card studio-panel"><h2>人物与复用身份</h2>{characters.length ? characters.map((character, index) => <article className="studio-asset-row" key={character.id || character.character_id || index}>{studioAssetPath(character) ? <img src={api.mediaUrl(studioAssetPath(character))} alt={recordText(character.display_name || character.name)}/> : <span className="studio-avatar-placeholder" aria-hidden="true">人</span>}<div><strong>{recordText(character.display_name || character.name) || recordText(character.character_id)}</strong><p>{recordText(character.description || character.identity || character.status)}</p><small>{recordText(character.character_id || character.id)}</small>{character.acceptance?.actor==='automation'&&character.current_approved_version_id!==character.version_id?<p className="studio-muted">自动技术采纳，待视频审片</p>:null}</div><Link to={`/characters/${character.character_id || character.id}?project=${encodeURIComponent(projectId)}`}>查看／修改</Link></article>) : <p>文档尚未建立人物关联。</p>}</section>
      <section className="card studio-panel"><h2>场景与环境素材</h2>{scenes.length ? scenes.map((scene, index) => <article className="studio-asset-row" key={scene.id || scene.scene_id || index}>{studioAssetPath(scene) ? <img src={api.mediaUrl(studioAssetPath(scene))} alt={recordText(scene.name || scene.title)}/> : null}<div><strong>{recordText(scene.name || scene.title || scene.scene_title)}</strong><p>{recordText(scene.description || scene.location || scene.status)}</p><small>{recordText(scene.scene_id || scene.id)}</small></div><Link to={`/storyboard?project=${encodeURIComponent(projectId)}`}>查看／修改</Link></article>) : <p>文档尚未建立场景记录。</p>}</section></div>
      <section id="project-output" className="card studio-panel studio-final-output">
        <div className="studio-section-heading"><h2>分镜与实际输出</h2></div>
        <section className="studio-complete-output" aria-label="完整成片">
          <div className="studio-output-subheading"><h3>完整成片</h3>{finalRender ? <StudioStatus status={finalRender.status}/> : null}</div>
          {finalPlayback ? <>
            <video className="studio-complete-video" key={finalRender?.id} src={finalPlayback} controls playsInline preload="metadata" aria-label="本次自动成片"/>
            {finalInfo?.width && finalInfo?.height && finalInfo?.fps ? <p>{finalInfo.width} × {finalInfo.height} · {finalInfo.fps} fps · {shotTime(finalInfo.duration)}</p> : null}
            {finalRender?.relative_path ? <a className="button button-primary" href={api.mediaDownloadUrl(finalRender.relative_path)} download><Download size={15}/>下载完整成片</a> : null}
          </> : <p role="status">{recordText(data.run?.error_message) || (data.run?.status === 'rendering' ? '正在合成成片。' : data.run ? '本次制作尚未产出成片。' : '开始制作后，成片会在这里显示。')}</p>}
          {retryFinal ? <button type="button" className="button button-secondary" disabled={pageBusy} onClick={() => void perform('retry-assembly', async () => {
            await request(`/api/studio/projects/${encodeURIComponent(projectId)}/retry-assembly`, {method:'POST',body:JSON.stringify({expected_run_id:data.run?.id})});
            setNotice('已提交成片重试，将复用本次通过技术检查的分镜。');
          })}>重试成片</button> : null}
          {previousExports.length ? <details className="workspace-details"><summary>历史成片</summary><ul>{previousExports.map(render => <li key={render.id}><a href={api.mediaDownloadUrl(render.relative_path)} download>{recordText(render.snapshot_json?.assembly_name) || '成片'} · {render.created_at ? new Date(render.created_at).toLocaleString() : render.id}</a></li>)}</ul></details> : null}
        </section>
        <div className="studio-output-subheading"><h3>分镜处理状态</h3></div>
        {shots.length ? <div className="studio-shot-table">{shots.map(shot => {
          return <article key={shot.id}>
            <strong>#{shot.order} · {shot.title || shot.scene_name}</strong>
            <span>{shot.scene_name} · {shot.cast?.map(person => person.name).join('、') || '未关联人物'}</span>
            <span>{shotTime(studioVideoDuration(shot.video) || shot.duration_seconds)}</span>
            <StudioStatus status={shot.status}/>
          </article>;
        })}</div> : <EmptyState title="还没有分镜" description="上传包含场景与人物动作的 Word 剧本，自动建立分镜和关联。"/>}
      </section>
      <details className="workspace-details"><summary>项目剧本与高级编辑</summary><p>{recordText(data.script?.raw_text || data.project.description).slice(0,1200) || '可通过下方入口查看源文档、人物和逐镜完整信息。'}</p><div className="studio-actions"><Link to={`/storyboard?project=${projectId}`}>源剧本与分镜</Link><Link to={`/storyboard?project=${encodeURIComponent(projectId)}`}>分镜设置与生成依据</Link><Link to={`/storyboard?project=${encodeURIComponent(projectId)}`}>查看分镜任务</Link><Link to="/content">历史与内容管理</Link></div></details>
    </> : null}
  </div>;
}
