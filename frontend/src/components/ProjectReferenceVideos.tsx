import { useEffect, useMemo, useState } from 'react';
import { api, request } from '../lib/api';
import type { ReferenceVideoAsset, ReferenceVideoOverview } from '../lib/asset-library-contract';
import type { StudioShot } from '../lib/studio-contract';
import { FormField } from './ui';

export type ReferenceVideoRole = 'action' | 'camera' | 'style' | 'combined';

export type ReferenceVideoBinding = {
  video_id: string;
  expected_revision: number;
  source_sha256: string;
  trim_start_seconds: number;
  trim_end_seconds: number;
  reference_role: ReferenceVideoRole;
  include_audio: boolean;
  authorized_for_reference: boolean;
};

type SavedReferenceVideos = {
  project_id: string;
  revision: number;
  reference_video_defaults?: ReferenceVideoBinding[];
  shot_reference_video_overrides?: Record<string, ReferenceVideoBinding[]>;
};

type ProjectReferenceVideoState = { busy: boolean; dirty: boolean };

const ROLE_LABELS: Record<ReferenceVideoRole, string> = {
  action: '动作模仿',
  camera: '运镜模仿',
  style: '画面风格',
  combined: '综合参考',
};

const emptyPlan = (projectId: string): SavedReferenceVideos => ({
  project_id: projectId,
  revision: 0,
  reference_video_defaults: [],
  shot_reference_video_overrides: {},
});

function cloneBindings(items: ReferenceVideoBinding[]) {
  return items.map(item => ({ ...item }));
}

function usableDuration(video: ReferenceVideoAsset) {
  const value = Number(video.metadata?.duration_seconds);
  return Number.isFinite(value) && value > 0 ? value : 0;
}

function newBinding(video: ReferenceVideoAsset): ReferenceVideoBinding {
  const end = usableDuration(video);
  return {
    video_id: video.id,
    expected_revision: video.revision,
    source_sha256: video.sha256,
    trim_start_seconds: 0,
    trim_end_seconds: end,
    reference_role: 'combined',
    include_audio: false,
    authorized_for_reference: true,
  };
}

function bindingIssue(binding: ReferenceVideoBinding, video?: ReferenceVideoAsset) {
  const start = Number(binding.trim_start_seconds);
  const end = Number(binding.trim_end_seconds);
  if (!Number.isFinite(start) || !Number.isFinite(end) || start < 0 || end <= start) {
    return '每段参考视频必须填写有效起止时间。';
  }
  if (video && end > usableDuration(video) + 0.0001) return '截取结束时间不能超过原视频时长。';
  if (video && (video.revision !== binding.expected_revision || video.sha256 !== binding.source_sha256)) {
    return '此视频的版本已变化。请重新选择后再保存。';
  }
  if (binding.include_audio && video && !video.metadata?.has_audio) return '该视频没有可作为参考的音轨。';
  return '';
}

function bindingsIssue(bindings: ReferenceVideoBinding[], videos: ReferenceVideoAsset[]) {
  if (bindings.length > 3) return 'MiniMax H3 一次最多接收 3 段参考视频。';
  const ids = new Set<string>();
  for (const binding of bindings) {
    if (ids.has(binding.video_id)) return '同一层级不能重复选择同一个参考视频。';
    ids.add(binding.video_id);
    const issue = bindingIssue(binding, videos.find(video => video.id === binding.video_id));
    if (issue) return issue;
  }
  return '';
}

function samePlan(saved: SavedReferenceVideos, defaults: ReferenceVideoBinding[], overrides: Record<string, ReferenceVideoBinding[]>) {
  return JSON.stringify({ defaults: saved.reference_video_defaults || [], overrides: saved.shot_reference_video_overrides || {} })
    === JSON.stringify({ defaults, overrides });
}

function bindingLocations(
  shots: StudioShot[],
  defaults: ReferenceVideoBinding[],
  overrides: Record<string, ReferenceVideoBinding[]>,
) {
  const locations: Record<string, string[]> = {};
  const add = (videoId: string, label: string) => {
    locations[videoId] = [...(locations[videoId] || []), label];
  };
  const inheritingShots = shots.filter(shot => !Object.prototype.hasOwnProperty.call(overrides, shot.id));
  for (const binding of defaults) {
    add(binding.video_id, inheritingShots.length ? `项目默认（${inheritingShots.length} 镜继承）` : '项目默认（当前无镜头继承）');
  }
  for (const shot of shots) {
    for (const binding of overrides[shot.id] || []) add(binding.video_id, `第 ${shot.order} 镜`);
  }
  return locations;
}

function inputNumber(value: number, update: (value: number) => void, disabled: boolean, label: string) {
  return <FormField label={label}><input type="number" min="0" step="0.001" value={Number.isFinite(value) ? value : ''} disabled={disabled}
    onChange={event => update(Number(event.target.value))}/></FormField>;
}

export function ProjectReferenceVideos({ projectId, shots, disabled, onStateChange, onSaved }: {
  projectId: string;
  shots: StudioShot[];
  disabled: boolean;
  onStateChange: (state: ProjectReferenceVideoState) => void;
  onSaved: () => Promise<void>;
}) {
  const [saved, setSaved] = useState<SavedReferenceVideos | null>(null);
  const [defaults, setDefaults] = useState<ReferenceVideoBinding[]>([]);
  const [overrides, setOverrides] = useState<Record<string, ReferenceVideoBinding[]>>({});
  const [library, setLibrary] = useState<ReferenceVideoAsset[]>([]);
  const [availableLibrary, setAvailableLibrary] = useState<ReferenceVideoAsset[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [defaultChoice, setDefaultChoice] = useState('');
  const [shotChoices, setShotChoices] = useState<Record<string, string>>({});
  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadKey, setUploadKey] = useState(0);
  const [existingChoice, setExistingChoice] = useState('');
  const [reload, setReload] = useState(0);

  const activeVideos = useMemo(() => library.filter(video => video.state === 'active'), [library]);
  const dirty = saved !== null && !samePlan(saved, defaults, overrides);
  const locations = useMemo(() => bindingLocations(shots, defaults, overrides), [defaults, overrides, shots]);
  useEffect(() => onStateChange({ busy: busy || loading, dirty }), [busy, dirty, loading, onStateChange]);
  useEffect(() => () => onStateChange({ busy: false, dirty: false }), [onStateChange]);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError('');
    void Promise.all([
      request<SavedReferenceVideos>(`/api/studio/projects/${encodeURIComponent(projectId)}/reference-videos`, { signal: controller.signal }),
      request<ReferenceVideoOverview>(`/api/studio/projects/${encodeURIComponent(projectId)}/reference-videos/library`, { signal: controller.signal }),
    ]).then(([configuration, overview]) => {
      if (controller.signal.aborted) return;
      const next = { ...emptyPlan(projectId), ...configuration };
      const nextDefaults = cloneBindings(next.reference_video_defaults || []);
      const nextOverrides = Object.fromEntries(Object.entries(next.shot_reference_video_overrides || {})
        .map(([shotId, bindings]) => [shotId, cloneBindings(bindings)]));
      const visible = Array.isArray(overview.items) ? overview.items : [];
      setSaved(next);
      setLibrary(visible);
      setDefaults(nextDefaults);
      setOverrides(nextOverrides);
      if (!nextDefaults.length && !Object.keys(nextOverrides).length && visible.filter(video => video.state === 'active').length === 1) {
        const only = visible.find(video => video.state === 'active');
        if (only && usableDuration(only) > 0) {
          setDefaults([newBinding(only)]);
          setNotice('已为全部分镜预填当前唯一的参考视频。请保存，或为某个镜头设置覆盖。');
        }
      }
    }).catch(cause => {
      if (!controller.signal.aborted) setError(String(cause));
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false);
    });
    return () => controller.abort();
  }, [projectId, reload]);

  function addBinding(scope: 'default' | string) {
    const selected = activeVideos.find(video => video.id === (scope === 'default' ? defaultChoice : shotChoices[scope]));
    if (!selected) return;
    const current = scope === 'default' ? defaults : overrides[scope] || [];
    if (current.some(binding => binding.video_id === selected.id) || current.length >= 3) return;
    if (scope === 'default') {
      setDefaults([...current, newBinding(selected)]);
      setDefaultChoice('');
    } else {
      setOverrides(previous => ({ ...previous, [scope]: [...current, newBinding(selected)] }));
      setShotChoices(previous => ({ ...previous, [scope]: '' }));
    }
  }

  function updateBinding(scope: 'default' | string, index: number, next: ReferenceVideoBinding) {
    if (scope === 'default') setDefaults(previous => previous.map((binding, position) => position === index ? next : binding));
    else setOverrides(previous => ({ ...previous, [scope]: (previous[scope] || []).map((binding, position) => position === index ? next : binding) }));
  }

  function removeBinding(scope: 'default' | string, index: number) {
    if (scope === 'default') setDefaults(previous => previous.filter((_, position) => position !== index));
    else setOverrides(previous => ({ ...previous, [scope]: (previous[scope] || []).filter((_, position) => position !== index) }));
  }

  function replaceBinding(scope: 'default' | string, index: number, videoId: string) {
    const selected = activeVideos.find(video => video.id === videoId);
    if (!selected) return;
    const current = scope === 'default' ? defaults : overrides[scope] || [];
    if (current.some((binding, position) => position !== index && binding.video_id === videoId)) return;
    updateBinding(scope, index, newBinding(selected));
  }

  async function save() {
    if (!saved || busy) return;
    const defaultProblem = bindingsIssue(defaults, activeVideos);
    if (defaultProblem) { setError(defaultProblem); return; }
    for (const bindings of Object.values(overrides)) {
      const problem = bindingsIssue(bindings, activeVideos);
      if (problem) { setError(problem); return; }
    }
    setBusy(true); setError('');
    try {
      const result = await request<SavedReferenceVideos>(`/api/studio/projects/${encodeURIComponent(projectId)}/reference-videos`, {
        method: 'PUT',
        body: JSON.stringify({
          expected_revision: saved.revision,
          reference_video_defaults: defaults,
          shot_reference_video_overrides: overrides,
        }),
      });
      const committed = { ...emptyPlan(projectId), ...result };
      setSaved(committed);
      setDefaults(cloneBindings(committed.reference_video_defaults || []));
      setOverrides(Object.fromEntries(Object.entries(committed.shot_reference_video_overrides || {})
        .map(([shotId, bindings]) => [shotId, cloneBindings(bindings)])));
      setNotice('参考视频设置已保存。后续新任务会冻结实际视频、片段、用途与音轨选择；已提交任务保持原快照。');
      await onSaved();
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }

  async function upload() {
    if (!uploadFile || busy) return;
    setBusy(true); setError('');
    try {
      const form = new FormData();
      form.set('file', uploadFile);
      const result = await request<{ item: ReferenceVideoAsset }>(`/api/studio/projects/${encodeURIComponent(projectId)}/reference-videos/upload`, { method: 'POST', body: form });
      if (!result.item?.id) throw new Error('服务器没有返回可选择的项目参考视频。');
      setLibrary(previous => [result.item, ...previous.filter(video => video.id !== result.item.id)]);
      setUploadFile(null); setUploadKey(value => value + 1);
      setNotice('参考视频原件已保存至当前项目。请在下方选择片段和用途。');
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }

  async function loadExistingLibrary() {
    if (busy) return;
    setBusy(true); setError('');
    try {
      const result = await request<ReferenceVideoOverview>(`/api/studio/projects/${encodeURIComponent(projectId)}/reference-videos/available`);
      setAvailableLibrary(Array.isArray(result.items) ? result.items : []);
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }

  async function attachExisting() {
    if (!existingChoice || busy) return;
    setBusy(true); setError('');
    try {
      const result = await request<{ item: ReferenceVideoAsset }>(`/api/studio/projects/${encodeURIComponent(projectId)}/reference-videos/${encodeURIComponent(existingChoice)}/attach`, { method: 'POST' });
      if (!result.item?.id) throw new Error('服务器没有返回已绑定到项目的参考视频。');
      setLibrary(previous => [result.item, ...previous.filter(video => video.id !== result.item.id)]);
      setAvailableLibrary(previous => previous?.map(video => video.id === result.item.id ? result.item : video) || previous);
      setExistingChoice('');
      setNotice('已明确将已有素材库视频添加到当前项目；它不会被其他项目自动继承。请在下方选择片段和用途。');
    } catch (cause) { setError(String(cause)); }
    finally { setBusy(false); }
  }

  const locked = disabled || busy || loading;
  return <section className="project-reference-videos" aria-label="可选参考视频">
    <div className="studio-section-heading"><div><h3>参考视频（可选）</h3><p>视频直接预处理为 24 fps 帧序列并接入 MiniMax H3 的原生 <code>ref_video_N</code> 输入。它不替代产品／人物图片；默认不使用原视频音轨。</p></div><button type="button" className="button button-secondary button-small" disabled={locked} onClick={() => setReload(value => value + 1)}>重新读取</button></div>
    <p className="form-hint">最多 3 段，每段可选择原视频中的任意有效范围，时长不设上限。所选区间会按 24 fps 保留完整原始时序并接入 H3 原生帧输入。图片与视频独立编号，用户指令和产品／人物参考优先于视频中的主体。</p>
    {loading ? <p role="status">读取本项目可用的参考视频…</p> : null}
    {error ? <p role="alert" className="inline-notice warning">{error}</p> : null}
    {notice ? <p role="status" className="inline-notice">{notice}</p> : null}
    <fieldset>
      <legend>上传到当前项目</legend>
      <FormField label="参考视频文件" hint="原文件会保留，后端另存经验证的 24 fps 模型输入副本。"><input key={uploadKey} type="file" accept=".mp4,.mov,.mkv,.webm,video/*" disabled={locked} onChange={event => setUploadFile(event.target.files?.[0] || null)}/></FormField>
      <button type="button" className="button button-secondary button-small" disabled={locked || !uploadFile} onClick={() => void upload()}>上传参考视频</button>
    </fieldset>
    <fieldset>
      <legend>从已有视频素材库明确添加</legend>
      <p className="form-hint">浏览不会授权或继承素材；只有点击“添加到当前项目”后，该视频才可被本项目的分镜选择。</p>
      {!availableLibrary ? <button type="button" className="button button-secondary button-small" disabled={locked} onClick={() => void loadExistingLibrary()}>读取已有视频素材库</button> : <div className="toolbar"><FormField label="已有素材库视频"><select value={existingChoice} disabled={locked} onChange={event => setExistingChoice(event.target.value)}><option value="">请选择</option>{availableLibrary.filter(video => video.state === 'active' && !video.linked_to_project && usableDuration(video) > 0).map(video => <option key={video.id} value={video.id}>{video.name} · {usableDuration(video).toFixed(2)} 秒</option>)}</select></FormField><button type="button" className="button button-secondary button-small" disabled={locked || !existingChoice} onClick={() => void attachExisting()}>添加到当前项目</button></div>}
    </fieldset>
    <ReferenceBindingScope title="项目默认参考视频" bindings={defaults} videos={activeVideos} locations={locations} choice={defaultChoice} onChoice={setDefaultChoice}
      disabled={locked} onAdd={() => addBinding('default')} onUpdate={(index, binding) => updateBinding('default', index, binding)} onReplace={(index, videoId) => replaceBinding('default', index, videoId)} onRemove={index => removeBinding('default', index)} />
    <section className="project-reference-shot-list">
      <h4>逐镜参考视频</h4>
      <p className="form-hint">未设置覆盖时继承项目默认；“本镜不使用”会明确取消继承。每个镜头的选择互不影响。</p>
      {shots.map(shot => {
        const explicit = Object.prototype.hasOwnProperty.call(overrides, shot.id);
        const bindings = overrides[shot.id] || [];
        return <details className="workspace-details project-reference-shot" key={shot.id}>
          <summary>第 {shot.order} 镜 · {shot.title || shot.scene_name || shot.id} · {explicit ? (bindings.length ? `本镜覆盖 ${bindings.length} 段` : '本镜不使用参考视频') : (defaults.length ? `继承项目默认 ${defaults.length} 段` : '未绑定')}</summary>
          {!explicit ? <p className="form-hint">当前使用项目默认。你可以创建此镜覆盖，或明确设为不使用视频。</p> : null}
          <div className="asset-actions"><button type="button" className="button button-secondary button-small" disabled={locked} onClick={() => setOverrides(previous => ({ ...previous, [shot.id]: explicit ? bindings : cloneBindings(defaults) }))}>创建／编辑本镜覆盖</button>
            {explicit ? <button type="button" className="button button-secondary button-small" disabled={locked} onClick={() => setOverrides(previous => { const next = { ...previous }; delete next[shot.id]; return next; })}>恢复项目默认</button> : null}
            <button type="button" className="button button-ghost-danger button-small" disabled={locked} onClick={() => setOverrides(previous => ({ ...previous, [shot.id]: [] }))}>本镜不使用视频</button></div>
          {explicit ? <ReferenceBindingScope title="本镜覆盖" bindings={bindings} videos={activeVideos} locations={locations} choice={shotChoices[shot.id] || ''} onChoice={value => setShotChoices(previous => ({ ...previous, [shot.id]: value }))}
            disabled={locked} onAdd={() => addBinding(shot.id)} onUpdate={(index, binding) => updateBinding(shot.id, index, binding)} onReplace={(index, videoId) => replaceBinding(shot.id, index, videoId)} onRemove={index => removeBinding(shot.id, index)} /> : null}
        </details>;
      })}
    </section>
    <div className="studio-actions"><button type="button" className="button button-primary" disabled={locked || !dirty} onClick={() => void save()}>保存参考视频设置</button>{dirty ? <button type="button" className="button button-secondary" disabled={locked} onClick={() => { if (saved) { setDefaults(cloneBindings(saved.reference_video_defaults || [])); setOverrides(Object.fromEntries(Object.entries(saved.shot_reference_video_overrides || {}).map(([shotId, bindings]) => [shotId, cloneBindings(bindings)]))); } }}>放弃未保存选择</button> : null}</div>
  </section>;
}

function ReferenceBindingScope({ title, bindings, videos, locations, choice, onChoice, disabled, onAdd, onUpdate, onReplace, onRemove }: {
  title: string;
  bindings: ReferenceVideoBinding[];
  videos: ReferenceVideoAsset[];
  locations: Record<string, string[]>;
  choice: string;
  onChoice: (value: string) => void;
  disabled: boolean;
  onAdd: () => void;
  onUpdate: (index: number, binding: ReferenceVideoBinding) => void;
  onReplace: (index: number, videoId: string) => void;
  onRemove: (index: number) => void;
}) {
  const problem = bindingsIssue(bindings, videos);
  return <fieldset className="project-reference-binding-scope"><legend>{title}</legend>
    {bindings.map((binding, index) => {
      const video = videos.find(item => item.id === binding.video_id);
      const issue = bindingIssue(binding, video);
      return <article className="project-reference-video" key={`${binding.video_id}:${index}`}>
        <div className="project-reference-video-preview">{video ? <video controls preload="metadata" src={api.mediaUrl(video.media_url || video.relative_path)}/> : <p>此视频不再可用。</p>}</div>
        <div className="project-reference-video-fields"><strong>Video {index + 1} · {video?.name || binding.video_id}</strong>
          {video ? <p>{usableDuration(video).toFixed(3)} 秒 · {video.metadata.width || '—'} × {video.metadata.height || '—'} · {video.metadata.fps?.toFixed(2) || '—'} fps · {video.metadata.has_audio ? '含音轨' : '无音轨'}</p> : null}
          {locations[binding.video_id]?.length ? <p>绑定镜头：{locations[binding.video_id].join('；')}</p> : null}
          <div className="workspace-grid"><FormField label={`Video ${index + 1} 用途`}><select value={binding.reference_role} disabled={disabled} onChange={event => onUpdate(index, { ...binding, reference_role: event.target.value as ReferenceVideoRole })}>{(Object.keys(ROLE_LABELS) as ReferenceVideoRole[]).map(role => <option key={role} value={role}>{ROLE_LABELS[role]}</option>)}</select></FormField>
            {inputNumber(binding.trim_start_seconds, value => onUpdate(index, { ...binding, trim_start_seconds: value }), disabled, `Video ${index + 1} 开始秒`)}
            {inputNumber(binding.trim_end_seconds, value => onUpdate(index, { ...binding, trim_end_seconds: value }), disabled, `Video ${index + 1} 结束秒`)}</div>
          <FormField label={`替换 Video ${index + 1}`}><select value="" disabled={disabled} onChange={event => onReplace(index, event.target.value)}><option value="">保留当前视频</option>{videos.filter(candidate => candidate.id !== binding.video_id && !bindings.some((item, position) => position !== index && item.video_id === candidate.id)).map(candidate => <option key={candidate.id} value={candidate.id}>{candidate.name} · {usableDuration(candidate).toFixed(2)} 秒</option>)}</select></FormField>
          <label className="studio-check"><input type="checkbox" checked={binding.include_audio} disabled={disabled || !video?.metadata.has_audio} onChange={event => onUpdate(index, { ...binding, include_audio: event.target.checked })}/>将本段原视频音轨作为 <code>ref_video_audio_{index + 1}</code> 输入（默认关闭）</label>
          {issue ? <p role="alert">{issue}</p> : null}<button type="button" className="button button-ghost-danger button-small" disabled={disabled} onClick={() => onRemove(index)}>移除此视频</button>
        </div>
      </article>;
    })}
    {problem ? <p role="alert">{problem}</p> : null}
    <div className="toolbar"><FormField label="从本项目视频库选择"><select value={choice} disabled={disabled || bindings.length >= 3} onChange={event => onChoice(event.target.value)}><option value="">请选择</option>{videos.filter(video => usableDuration(video) > 0 && !bindings.some(binding => binding.video_id === video.id)).map(video => <option key={video.id} value={video.id}>{video.name} · {usableDuration(video).toFixed(2)} 秒</option>)}</select></FormField><button type="button" className="button button-secondary button-small" disabled={disabled || !choice || bindings.length >= 3} onClick={onAdd}>添加视频</button></div>
  </fieldset>;
}
