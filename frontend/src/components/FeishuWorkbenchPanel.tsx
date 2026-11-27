import { useCallback, useEffect, useState } from 'react';
import { CloudDownload, Play, RefreshCw, Sparkles, X, CheckCircle2, AlertTriangle, FolderArchive } from 'lucide-react';
import { request } from '../lib/api';
import { Badge, FormField } from './ui';

type Row = Record<string, any>;

export type FeishuStatusPayload = {
  service: {
    configured: boolean;
    error?: string;
    service_id?: string;
    name?: string;
    bitable_url?: string;
    app_token?: string;
    table_id?: string;
    view_id?: string;
    analysis_engine?: 'antigravity' | 'codex';
    script_count?: number;
    analysis_prompt?: string;
    local_sync_dir?: string;
    local_archive_dir?: string;
    columns?: Record<string, string>;
  };
  bridge: {
    online: boolean;
    hostname: string;
    sync_dir: string;
    archive_dir: string;
    available_engines: string[];
    rea_video_ready: boolean;
    last_seen_at: string | null;
  };
  latest_sync_job: Row | null;
  records: Row[];
  batches: Row[];
};

const syncStatusLabels: Record<string, string> = {
  queued: '等待本机桥接认领',
  claimed: '本机桥接已认领',
  downloading: '正在拉取飞书素材至 E:\\feishu_sync',
  analyzing: '正在运行 rea-video 拆解并生成脚本',
  writing_feishu: '正在回写脚本至飞书表格',
  completed: '同步完成',
  failed: '同步失败',
  cancelled: '已取消',
};

const batchStatusLabels: Record<string, string> = {
  queued: '排队中',
  running: '串行生成中',
  ready_for_download: '全部视频已生成 · 等待本机自动下载归档',
  downloading: '正在自动下载至 E:\\vast视频 并按产品归档',
  archived: '已全部下载并按产品文件夹归档',
  failed: '批次存在失败',
  cancelled: '已取消',
};

export function FeishuWorkbenchPanel({
  mode: initialMode,
  onClose,
  onProjectsChanged,
}: {
  mode: 'sync' | 'generate';
  onClose: () => void;
  onProjectsChanged?: () => void;
}) {
  const [mode, setMode] = useState<'sync' | 'generate'>(initialMode);
  useEffect(() => { setMode(initialMode); }, [initialMode]);
  const [status, setStatus] = useState<FeishuStatusPayload | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [forceRerun, setForceRerun] = useState(false);
  const [engineChoice, setEngineChoice] = useState<'antigravity' | 'codex'>('codex');
  const [engineTouched, setEngineTouched] = useState(false);
  const [previewItems, setPreviewItems] = useState<Row[]>([]);
  const [selectedKeys, setSelectedKeys] = useState<string[]>([]);
  const [previewLoaded, setPreviewLoaded] = useState(false);
  const [aspectRatio, setAspectRatio] = useState<'9:16' | '16:9'>('9:16');
  const [resolution, setResolution] = useState<'4k' | '1080p' | '720p'>('4k');

  const keyOf = (item: Row) => `${item.sync_record_id}::${item.script_index}`;

  const loadStatus = useCallback(async () => {
    try {
      const data = await request<FeishuStatusPayload>('/api/feishu/status');
      setStatus(data);
      if (!engineTouched && data.service?.analysis_engine) {
        setEngineChoice(data.service.analysis_engine);
      }
      setError('');
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [engineTouched]);

  const loadPreview = useCallback(async (refreshFromFeishu = true) => {
    setBusy(true);
    setError('');
    try {
      const data = await request<{ items: Row[]; total: number }>('/api/feishu/generate/preview', {
        method: 'POST',
        body: JSON.stringify({ refresh_from_feishu: refreshFromFeishu }),
      });
      setPreviewItems(data.items || []);
      const defaultSelected = (data.items || [])
        .filter((x) => !x.already_generated)
        .map(keyOf);
      setSelectedKeys(defaultSelected.length ? defaultSelected : (data.items || []).map(keyOf));
      setPreviewLoaded(true);
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void loadStatus();
    if (mode === 'generate') {
      void loadPreview(true);
    }
  }, [mode, loadStatus, loadPreview]);

  // Poll every 3s while panel is open so sync progress / serial generation queue / archiving update live
  useEffect(() => {
    const timer = window.setInterval(() => {
      void loadStatus();
    }, 3000);
    return () => window.clearInterval(timer);
  }, [loadStatus]);

  async function startSync() {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const job = await request<Row>('/api/feishu/sync', {
        method: 'POST',
        body: JSON.stringify({ force_rerun: forceRerun, engine: engineChoice }),
      });
      setNotice(`已发起飞书同步任务 ${job.id}（引擎：${engineChoice === 'antigravity' ? 'Antigravity' : 'Codex'}）`);
      await loadStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function cancelSync(jobId: string) {
    setBusy(true);
    try {
      await request(`/api/feishu/sync/${encodeURIComponent(jobId)}/cancel`, { method: 'POST' });
      await loadStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function startGenerateBatch() {
    setBusy(true);
    setError('');
    setNotice('');
    try {
      const selections = previewItems
        .filter((item) => selectedKeys.includes(keyOf(item)))
        .map((item) => ({ sync_record_id: item.sync_record_id, script_index: item.script_index }));
      const dims = aspectRatio === '9:16' ? { width: 576, height: 1024 } : { width: 1024, height: 576 };
      const batch = await request<Row>('/api/feishu/generate', {
        method: 'POST',
        body: JSON.stringify({
          selections: selections.length ? selections : null,
          include_already_generated: selections.length > 0,
          refresh_from_feishu: true,
          quality: {
            resolution,
            mode: 'fast',
            aspect_ratio: aspectRatio,
            processing: resolution === '4k' ? 'ai_super_resolution' : 'native',
          },
          ...dims,
        }),
      });
      setNotice(`已创建批量生成队列 ${batch.id}（共 ${batch.total_items} 条视频），全部完成后将自动下载至 ${batch.archive_dir} 并按产品名归档`);
      onProjectsChanged?.();
      await loadStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function redownloadBatch(batchId: string) {
    setBusy(true);
    setError('');
    try {
      await request(`/api/feishu/batches/${encodeURIComponent(batchId)}/redownload`, { method: 'POST' });
      setNotice('已重新触发本机自动下载与产品目录归档');
      await loadStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function cancelBatch(batchId: string) {
    setBusy(true);
    try {
      await request(`/api/feishu/batches/${encodeURIComponent(batchId)}/cancel`, { method: 'POST' });
      await loadStatus();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  }

  const svcCfg = status?.service;
  const bridge = status?.bridge;
  const latestJob = status?.latest_sync_job;
  const syncActive = Boolean(latestJob && !['completed', 'failed', 'cancelled'].includes(latestJob.status));
  const latestBatch = status?.batches?.[0] || null;

  return (
    <section className="card studio-panel" aria-label={mode === 'sync' ? '一键同步飞书' : '一键生成视频'}>
      <div className="section-heading compact">
        <div>
          <p className="eyebrow">飞书多维表格自动化 · {mode === 'sync' ? '同步与脚本拆解' : '批量入队生成与自动归档'}</p>
          <h2>{mode === 'sync' ? '一键同步飞书（rea-video 解析并回写脚本列）' : '一键生成视频（自动入队 & 按产品归档至 E:\\vast视频）'}</h2>
        </div>
        <div className="toolbar">
          <div className="segmented">
            <button type="button" className={mode === 'generate' ? 'active' : ''} onClick={() => setMode('generate')}>一键生成视频</button>
            <button type="button" className={mode === 'sync' ? 'active' : ''} onClick={() => setMode('sync')}>同步飞书与脚本拆解</button>
          </div>
          <Badge tone={bridge?.online ? 'success' : 'warning'}>
            {bridge?.online ? `本机桥接在线 (${bridge.hostname || 'Windows'})` : '本机桥接未连接'}
          </Badge>
          <button type="button" className="button button-secondary button-small" onClick={() => void loadStatus()}>
            <RefreshCw size={14} />刷新状态
          </button>
          <button type="button" className="button button-ghost button-small" onClick={onClose}>
            <X size={15} />关闭
          </button>
        </div>
      </div>

      {!bridge?.online ? (
        <div className="inline-notice warning" role="alert">
          <AlertTriangle size={16} />
          <p>
            未检测到本机 Windows 桥接守护程序心跳。请在本地电脑运行 <code>E:\feishu_sync\启动飞书同步桥接.bat</code>，用于拉取附件到 <code>E:\feishu_sync</code>、调用本机 Antigravity / Codex 运行 <code>rea-video</code>，以及在整批生成完成后自动下载归档到 <code>E:\vast视频</code>。
          </p>
        </div>
      ) : (
        <p className="form-hint">
          本机同步目录：<code>{bridge.sync_dir}</code> · 归档目录：<code>{bridge.archive_dir}</code> ·
          可用引擎：{bridge.available_engines.join(' / ') || '未上报'} ·
          rea-video：{bridge.rea_video_ready ? '已就绪' : '待安装'}
        </p>
      )}

      {loading ? <p>正在读取飞书配置与同步状态…</p> : null}
      {error ? <p role="alert" className="inline-notice warning">{error}</p> : null}
      {notice ? <p role="status" className="inline-notice">{notice}</p> : null}

      {svcCfg && !svcCfg.configured ? (
        <div className="inline-notice warning">
          <p>{svcCfg.error || '尚未配置飞书多维表格服务档案。'}</p>
          <a className="button button-primary button-small" href="/settings">前往系统设置配置飞书</a>
        </div>
      ) : null}

      {mode === 'sync' && svcCfg?.configured ? (
        <div className="feishu-sync-box">
          <div className="editor-form-grid">
            <FormField label="视频分析执行引擎（手动选择）">
              <select
                value={engineChoice}
                disabled={busy || syncActive}
                onChange={(e) => {
                  setEngineChoice(e.target.value as 'antigravity' | 'codex');
                  setEngineTouched(true);
                }}
              >
                <option value="antigravity">Antigravity（使用本机 rea-video 解析并生成脚本）</option>
                <option value="codex">Codex（使用本机 rea-video 解析并生成脚本）</option>
              </select>
            </FormField>
            <FormField label="目标飞书表格与脚本数量">
              <p className="form-hint">
                表 <code>{svcCfg.table_id}</code> · 生成 <strong>{svcCfg.script_count}</strong> 份脚本（从「{svcCfg.columns?.col_script_start || '脚本1'}」起自动补建列）
              </p>
            </FormField>
          </div>
          {svcCfg.analysis_prompt ? (
            <details>
              <summary>当前分析视频提示词内容要求（可在「系统设置 → 飞书配置」修改）</summary>
              <pre className="source-blocks">{svcCfg.analysis_prompt}</pre>
            </details>
          ) : (
            <p className="form-hint">提示：你尚未在系统设置中填写「分析视频提示词内容要求」，将按默认分镜改写指令执行。</p>
          )}
          <label className="studio-check">
            <input type="checkbox" checked={forceRerun} onChange={(e) => setForceRerun(e.target.checked)} disabled={busy || syncActive} />
            <span>强制重新分析并覆盖飞书中已有脚本列（默认跳过已生成完整脚本的行）</span>
          </label>
          <div className="toolbar">
            <button type="button" className="button button-primary" disabled={busy || syncActive} onClick={() => void startSync()}>
              <Sparkles size={15} />{syncActive ? '同步进行中…' : '立即一键同步飞书'}
            </button>
            {syncActive && latestJob ? (
              <button type="button" className="button button-secondary" disabled={busy} onClick={() => void cancelSync(latestJob.id)}>
                取消当前同步
              </button>
            ) : null}
            <a className="button button-ghost" href="/settings">修改飞书与提示词配置</a>
          </div>

          {latestJob ? (
            <div className="inline-notice" style={{ marginTop: 12 }}>
              <div>
                <strong>最近同步任务 {latestJob.id}</strong> ·{' '}
                <Badge tone={latestJob.status === 'completed' ? 'success' : latestJob.status === 'failed' ? 'danger' : 'primary'}>
                  {syncStatusLabels[latestJob.status] || latestJob.status}
                </Badge>{' '}
                · 引擎：{latestJob.engine} · 进度：{latestJob.progress?.processed_records || 0}/{latestJob.progress?.total_records || 0}
                {latestJob.progress?.current_stage ? <p className="form-hint">{latestJob.progress.current_stage}</p> : null}
                {latestJob.error_message ? <p role="alert">{latestJob.error_message}</p> : null}
                {(latestJob.progress?.logs || []).length ? (
                  <details open={syncActive}>
                    <summary>同步执行日志 ({latestJob.progress.logs.length})</summary>
                    <pre className="source-blocks">{(latestJob.progress.logs as string[]).join('\n')}</pre>
                  </details>
                ) : null}
              </div>
            </div>
          ) : null}

          {(status?.records || []).length > 0 ? (
            <details open style={{ marginTop: 12 }}>
              <summary>已同步的飞书素材与脚本 ({status!.records.length} 条记录)</summary>
              <table className="management-table">
                <thead>
                  <tr>
                    <th>产品名称</th>
                    <th>任务名称</th>
                    <th>素材状态</th>
                    <th>已回写脚本</th>
                    <th>引擎</th>
                  </tr>
                </thead>
                <tbody>
                  {status!.records.map((rec) => (
                    <tr key={rec.id}>
                      <td><strong>{rec.product_name}</strong><small>{rec.feishu_record_id}</small></td>
                      <td>{rec.task_name}</td>
                      <td>
                        视频：{rec.reference_video_id ? '已入库' : '缺失'} · 参考图：{rec.product_image_ids?.length || 0} 张
                        {rec.reference_video_local_path ? <small>{rec.reference_video_local_path}</small> : null}
                      </td>
                      <td>{(rec.scripts || []).length} 份</td>
                      <td>{rec.engine_used || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </details>
          ) : null}
        </div>
      ) : null}

      {mode === 'generate' ? (
        <div className="feishu-generate-box">
          <div className="editor-form-grid">
            <FormField label="画幅比例">
              <select value={aspectRatio} onChange={(e) => setAspectRatio(e.target.value as '9:16' | '16:9')} disabled={busy}>
                <option value="9:16">9:16 竖屏（576×1024）</option>
                <option value="16:9">16:9 横屏（1024×576）</option>
              </select>
            </FormField>
            <FormField label="成片分辨率">
              <select value={resolution} onChange={(e) => setResolution(e.target.value as '4k' | '1080p' | '720p')} disabled={busy}>
                <option value="4k">4K AI 超分（推荐）</option>
                <option value="1080p">1080p</option>
                <option value="720p">720p 原生</option>
              </select>
            </FormField>
          </div>

          <div className="toolbar" style={{ marginBottom: 10 }}>
            <button type="button" className="button button-secondary" disabled={busy} onClick={() => void loadPreview(true)}>
              <RefreshCw size={14} />从飞书刷新最新脚本
            </button>
            <button
              type="button"
              className="button button-primary"
              disabled={busy || !selectedKeys.length}
              onClick={() => void startGenerateBatch()}
            >
              <Play size={15} />一键加入生成队列（已选 {selectedKeys.length} 条）
            </button>
          </div>

          {previewLoaded && !previewItems.length ? (
            <p className="form-hint">暂无可生成的飞书脚本。请先点击左侧「一键同步飞书」拉取参考视频、参考图并生成脚本。</p>
          ) : null}

          {previewItems.length > 0 ? (
            <table className="management-table">
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      aria-label="全选脚本"
                      checked={selectedKeys.length === previewItems.length}
                      onChange={(e) => setSelectedKeys(e.target.checked ? previewItems.map(keyOf) : [])}
                    />
                  </th>
                  <th>产品名称（归档目录）</th>
                  <th>任务名称 / 脚本列</th>
                  <th>参考素材</th>
                  <th>脚本摘要</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>
                {previewItems.map((item) => {
                  const k = keyOf(item);
                  return (
                    <tr key={k}>
                      <td>
                        <input
                          type="checkbox"
                          checked={selectedKeys.includes(k)}
                          onChange={(e) =>
                            setSelectedKeys((cur) => (e.target.checked ? [...cur, k] : cur.filter((x) => x !== k)))
                          }
                        />
                      </td>
                      <td><strong>{item.product_name}</strong></td>
                      <td>{item.task_name} · <Badge tone="neutral">{item.script_column}</Badge></td>
                      <td>{item.has_reference_video ? '视频✓' : '无视频'} / 图 {item.reference_image_count} 张</td>
                      <td><small>{item.script_preview}…</small></td>
                      <td>{item.already_generated ? <Badge tone="success">曾生成</Badge> : <Badge tone="primary">待生成</Badge>}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          ) : null}

          {latestBatch ? (
            <div className="inline-notice" style={{ marginTop: 14 }}>
              <div style={{ width: '100%' }}>
                <div className="section-heading compact">
                  <strong>
                    <FolderArchive size={15} /> 最新批量队列 {latestBatch.id} ·{' '}
                    <Badge tone={latestBatch.status === 'archived' ? 'success' : latestBatch.status === 'failed' ? 'danger' : 'primary'}>
                      {batchStatusLabels[latestBatch.status] || latestBatch.status}
                    </Badge>
                  </strong>
                  <div className="toolbar">
                    {['archived', 'ready_for_download', 'downloading'].includes(latestBatch.status) ? (
                      <button type="button" className="button button-secondary button-small" disabled={busy} onClick={() => void redownloadBatch(latestBatch.id)}>
                        <CloudDownload size={14} />重新下载归档至 {latestBatch.archive_dir}
                      </button>
                    ) : null}
                    {['queued', 'running'].includes(latestBatch.status) ? (
                      <button type="button" className="button button-ghost-danger button-small" disabled={busy} onClick={() => void cancelBatch(latestBatch.id)}>
                        取消未开始任务
                      </button>
                    ) : null}
                  </div>
                </div>
                <p className="form-hint">
                  进度：{latestBatch.completed_items}/{latestBatch.total_items} 已完成（失败 {latestBatch.failed_items}）· 归档根目录：<code>{latestBatch.archive_dir}</code>（按产品名称自动建子文件夹）
                </p>
                <table className="management-table">
                  <thead>
                    <tr>
                      <th>顺序</th>
                      <th>产品 / 任务 / 脚本</th>
                      <th>队列状态</th>
                      <th>项目 / 归档路径</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(latestBatch.items || []).map((it: Row) => (
                      <tr key={it.id}>
                        <td>#{it.order_index}</td>
                        <td><strong>{it.product_name}</strong> · {it.task_name} · {it.script_column}</td>
                        <td>
                          <Badge tone={it.status === 'completed' ? 'success' : it.status === 'failed' ? 'danger' : it.status === 'running' ? 'primary' : 'neutral'}>
                            {it.status}
                          </Badge>
                          {it.error_message ? <small>{it.error_message}</small> : null}
                        </td>
                        <td>
                          {it.project_id ? <a href={`/?project=${encodeURIComponent(it.project_id)}`}>打开项目</a> : '待建项'}
                          {it.archived_local_path ? (
                            <small><CheckCircle2 size={12} /> {it.archived_local_path}</small>
                          ) : null}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
