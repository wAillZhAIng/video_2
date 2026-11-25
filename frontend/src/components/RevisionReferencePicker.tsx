import { useEffect, useRef, useState } from 'react';
import { api, type RevisionImageDraft, type RevisionImageKind, type RevisionReferenceTargets } from '../lib/api';
import { FormField } from './ui';

const imageKinds = { character: '角色', product: '道具或商品' } as const;
const uploadLimit = 9;
const imageSizeLimit = 10 * 1024 * 1024;

export function RevisionReferencePicker({ videoId, images, disabled, onChange, onUploading }: {
  videoId: string;
  images: RevisionImageDraft[];
  disabled: boolean;
  onChange: (videoId: string, update: (current: RevisionImageDraft[]) => RevisionImageDraft[]) => void;
  onUploading: (videoId: string, uploading: boolean) => void;
}) {
  const [targets, setTargets] = useState<RevisionReferenceTargets | null>(null);
  const [targetError, setTargetError] = useState(false);
  const [uploadError, setUploadError] = useState('');
  const [uploading, setUploading] = useState(false);
  const activeUpload = useRef<AbortController | null>(null);
  const hasImages = images.length > 0;

  useEffect(() => () => activeUpload.current?.abort(), []);
  useEffect(() => {
    if (!hasImages) return;
    const controller = new AbortController();
    setTargetError(false);
    void api.revisionReferenceTargets(videoId, controller.signal).then(setTargets).catch(() => {
      if (!controller.signal.aborted) setTargetError(true);
    });
    return () => controller.abort();
  }, [hasImages, videoId]);

  async function addFiles(kind: RevisionImageKind, files: File[]) {
    if (disabled || activeUpload.current || !files.length) return;
    if (files.length + images.length > uploadLimit) {
      setUploadError('本次最多添加 9 张图片。原镜头保留的参考也计入实际提交上限。');
      return;
    }
    const controller = new AbortController();
    activeUpload.current = controller;
    const errors: string[] = [];
    setUploadError('');
    setUploading(true);
    onUploading(videoId, true);
    try {
      for (const file of files) {
        if (controller.signal.aborted) break;
        if (file.size > imageSizeLimit) {
          errors.push(`${file.name} 超过 10 MiB。`);
          continue;
        }
        try {
          const image = await api.uploadRevisionImage(videoId, kind, file, controller.signal);
          if (!controller.signal.aborted) {
            onChange(videoId, current => [...current, { ...image, targetId: '' }]);
          }
        } catch (error) {
          if (!controller.signal.aborted) errors.push(`${file.name} · ${error instanceof Error ? error.message : '上传失败'}`);
        }
      }
    } finally {
      activeUpload.current = null;
      onUploading(videoId, false);
      if (!controller.signal.aborted) {
        setUploading(false);
        setUploadError(errors.join(' '));
      }
    }
  }

  return <div className="revision-reference-picker">
    <div className="form-grid">
      {(['character', 'product'] as const).map(kind =>
        <FormField key={kind} label={`添加${imageKinds[kind]}图片（可选）`}>
          <input type="file" multiple accept=".png,.jpg,.jpeg,.webp"
            disabled={disabled || uploading || images.length >= uploadLimit}
            onChange={event => {
              const files = Array.from(event.currentTarget.files ?? []);
              event.currentTarget.value = '';
              void addFiles(kind, files);
            }}/>
        </FormField>)}
    </div>
    <p className="form-hint">可搭配文字或只用图片。手持商品图可作为独立外观参考；指定本镜对象时会替换该对象本次修订的参考。</p>
    {uploading ? <p role="status">正在上传修订图片…</p> : null}
    {uploadError ? <p role="alert">{uploadError}</p> : null}
    {hasImages && targetError ? <p className="form-hint">无法读取本镜对象。已上传的图片可作为独立外观参考。</p> : null}
    {hasImages ? <div className="reference-preview revision-reference-gallery" aria-label="本次修订图片">
      {images.map(image => <figure key={image.id}>
        <img src={api.mediaUrl(image.media_url)} alt={image.filename}/>
        <figcaption>{imageKinds[image.kind]} · {image.filename}</figcaption>
        {targets?.[image.kind]?.length ? <FormField label={`${image.filename} 的应用对象`}>
          <select value={image.targetId} disabled={disabled || uploading}
            onChange={event => {
              const targetId = event.target.value;
              onChange(videoId, current => current.map(item => item.id === image.id ? { ...item, targetId } : item));
            }}>
            <option value="">独立外观参考</option>
            {targets[image.kind].map(target => <option key={target.id} value={target.id}>{target.name}</option>)}
          </select>
        </FormField> : null}
        <button type="button" className="button button-secondary button-small" disabled={disabled || uploading}
          aria-label={`移除 ${image.filename}`}
          onClick={() => onChange(videoId, current => current.filter(item => item.id !== image.id))}>移除</button>
      </figure>)}
    </div> : null}
  </div>;
}
