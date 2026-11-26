import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { request } from '../lib/api';
import { ProjectReferenceVideos } from './ProjectReferenceVideos';

vi.mock('../lib/api', async original => ({ ...await original<typeof import('../lib/api')>(), request: vi.fn() }));

const mocked = vi.mocked(request);
const video = {
  id: 'ref-video-1', name: '手持水杯操作', filename: 'cup.mp4', category: 'demo', tags: [], notes: '', source_note: '', usage_scope: 'project',
  relative_path: 'reference/cup.mp4', media_url: '/api/media/reference/cup.mp4', sha256: 'a'.repeat(64),
  metadata: { duration_seconds: 8, width: 1080, height: 1920, fps: 30, has_audio: true }, state: 'active', revision: 3,
  created_at: '', updated_at: '',
};
const secondVideo = {
  ...video, id: 'ref-video-2', name: '车灯夜骑跟拍', filename: 'light.mp4', relative_path: 'reference/light.mp4',
  media_url: '/api/media/reference/light.mp4', sha256: 'b'.repeat(64), revision: 7,
  metadata: { duration_seconds: 120, width: 1920, height: 1080, fps: 24, has_audio: false },
};

afterEach(() => { cleanup(); mocked.mockReset(); vi.restoreAllMocks(); });

it('uses the sole project video as a saveable default and freezes its native Ref2VA inputs', async () => {
  const saved = vi.fn().mockResolvedValue(undefined);
  let configuration = { project_id: 'p1', revision: 4, reference_video_defaults: [], shot_reference_video_overrides: {} };
  mocked.mockImplementation(async (url, init) => {
    if (url === '/api/studio/projects/p1/reference-videos/library') return { items: [video], limits: {} };
    if (url === '/api/studio/projects/p1/reference-videos' && !init?.method) return structuredClone(configuration);
    if (url === '/api/studio/projects/p1/reference-videos' && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body));
      configuration = { ...configuration, revision: 5, reference_video_defaults: body.reference_video_defaults, shot_reference_video_overrides: body.shot_reference_video_overrides };
      return structuredClone(configuration);
    }
    throw new Error(`Unexpected request ${url}`);
  });
  const user = userEvent.setup();
  render(<ProjectReferenceVideos projectId="p1" shots={[{ id: 's1', order: 1, title: '产品拿取', duration_seconds: 5, status: 'approved', decision: {} }]} disabled={false} onStateChange={vi.fn()} onSaved={saved}/>);
  expect(await screen.findByText('已为全部分镜预填当前唯一的参考视频。请保存，或为某个镜头设置覆盖。')).toBeVisible();
  await user.selectOptions(screen.getByLabelText('Video 1 用途'), 'action');
  await user.click(screen.getByLabelText(/ref_video_audio_1/));
  await user.click(screen.getByRole('button', { name: '保存参考视频设置' }));
  await waitFor(() => expect(mocked.mock.calls.some(([url, init]) => url === '/api/studio/projects/p1/reference-videos' && init?.method === 'PUT')).toBe(true));
  const put = mocked.mock.calls.find(([url, init]) => url === '/api/studio/projects/p1/reference-videos' && init?.method === 'PUT')!;
  expect(JSON.parse(String(put[1]?.body))).toEqual({
    expected_revision: 4,
    reference_video_defaults: [{ video_id: 'ref-video-1', expected_revision: 3, source_sha256: 'a'.repeat(64), trim_start_seconds: 0, trim_end_seconds: 8, reference_role: 'action', include_audio: true, authorized_for_reference: true }],
    shot_reference_video_overrides: {},
  });
  expect(saved).toHaveBeenCalledOnce();
});

it('lets a shot explicitly opt out instead of accidentally inheriting the project default', async () => {
  mocked.mockImplementation(async url => {
    if (url === '/api/studio/projects/p1/reference-videos/library') return { items: [video], limits: {} };
    if (url === '/api/studio/projects/p1/reference-videos') return { project_id: 'p1', revision: 1, reference_video_defaults: [{ video_id: video.id, expected_revision: video.revision, source_sha256: video.sha256, trim_start_seconds: 0, trim_end_seconds: 8, reference_role: 'combined', include_audio: false }], shot_reference_video_overrides: {} };
    throw new Error(`Unexpected request ${url}`);
  });
  const user = userEvent.setup();
  render(<ProjectReferenceVideos projectId="p1" shots={[{ id: 's1', order: 1, title: '产品拿取', duration_seconds: 5, status: 'approved', decision: {} }]} disabled={false} onStateChange={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)}/>);
  await screen.findByText(/继承项目默认 1 段/);
  await user.click(screen.getByRole('button', { name: '本镜不使用视频' }));
  expect(screen.getByText(/本镜不使用参考视频/)).toBeVisible();
});

it('saves restore-project-default by removing the shot override from the replacement payload', async () => {
  let configuration = {
    project_id: 'p1', revision: 2,
    reference_video_defaults: [{ video_id: video.id, expected_revision: video.revision, source_sha256: video.sha256, trim_start_seconds: 0, trim_end_seconds: 8, reference_role: 'combined', include_audio: false, authorized_for_reference: true }],
    shot_reference_video_overrides: { s1: [{ video_id: video.id, expected_revision: video.revision, source_sha256: video.sha256, trim_start_seconds: 1, trim_end_seconds: 5, reference_role: 'camera', include_audio: false, authorized_for_reference: true }] },
  };
  mocked.mockImplementation(async (url, init) => {
    if (url === '/api/studio/projects/p1/reference-videos/library') return { items: [video], limits: {} };
    if (url === '/api/studio/projects/p1/reference-videos' && !init?.method) return structuredClone(configuration);
    if (url === '/api/studio/projects/p1/reference-videos' && init?.method === 'PUT') {
      const body = JSON.parse(String(init.body));
      configuration = { ...configuration, revision: 3, reference_video_defaults: body.reference_video_defaults, shot_reference_video_overrides: body.shot_reference_video_overrides };
      return structuredClone(configuration);
    }
    throw new Error(`Unexpected request ${url}`);
  });
  const user = userEvent.setup();
  render(<ProjectReferenceVideos projectId="p1" shots={[{ id: 's1', order: 1, title: '安装', duration_seconds: 5, status: 'approved', decision: {} }]} disabled={false} onStateChange={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)}/>);
  expect(await screen.findByText(/本镜覆盖 1 段/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: '恢复项目默认' }));
  await user.click(screen.getByRole('button', { name: '保存参考视频设置' }));
  await waitFor(() => expect(mocked.mock.calls.some(([url, init]) => url === '/api/studio/projects/p1/reference-videos' && init?.method === 'PUT')).toBe(true));
  const put = mocked.mock.calls.find(([url, init]) => url === '/api/studio/projects/p1/reference-videos' && init?.method === 'PUT')!;
  expect(JSON.parse(String(put[1]?.body)).shot_reference_video_overrides).toEqual({});
});

it('replaces a selected source with a fresh native-video snapshot instead of reusing its revision or audio setting', async () => {
  mocked.mockImplementation(async url => {
    if (url === '/api/studio/projects/p1/reference-videos/library') return { items: [video, secondVideo], limits: {} };
    if (url === '/api/studio/projects/p1/reference-videos') return { project_id: 'p1', revision: 1, reference_video_defaults: [{ video_id: video.id, expected_revision: video.revision, source_sha256: video.sha256, trim_start_seconds: 0, trim_end_seconds: 8, reference_role: 'combined', include_audio: true }], shot_reference_video_overrides: {} };
    throw new Error(`Unexpected request ${url}`);
  });
  const user = userEvent.setup();
  render(<ProjectReferenceVideos projectId="p1" shots={[]} disabled={false} onStateChange={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)}/>);
  await screen.findByText(/Video 1 · 手持水杯操作/);
  await user.selectOptions(screen.getByLabelText('替换 Video 1'), secondVideo.id);
  expect(screen.getByText(/Video 1 · 车灯夜骑跟拍/)).toBeVisible();
  expect(screen.getByLabelText(/ref_video_audio_1/)).not.toBeChecked();
  expect(screen.getByLabelText('Video 1 结束秒')).toHaveValue(120);
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
});

it('keeps a browsed global video out of project selections until the user explicitly attaches it', async () => {
  mocked.mockImplementation(async (url, init) => {
    if (url === '/api/studio/projects/p1/reference-videos/library') return { items: [], limits: {} };
    if (url === '/api/studio/projects/p1/reference-videos') return {
      project_id: 'p1', revision: 1, reference_video_defaults: [], shot_reference_video_overrides: {},
    };
    if (url === '/api/studio/projects/p1/reference-videos/available') {
      return { items: [{ ...video, linked_to_project: false }], limits: {} };
    }
    if (url === '/api/studio/projects/p1/reference-videos/ref-video-1/attach' && init?.method === 'POST') {
      return { item: { ...video, linked_to_project: true } };
    }
    throw new Error(`Unexpected request ${url}`);
  });
  const user = userEvent.setup();
  render(<ProjectReferenceVideos projectId="p1" shots={[]} disabled={false} onStateChange={vi.fn()} onSaved={vi.fn().mockResolvedValue(undefined)}/>);

  const projectPicker = await screen.findByLabelText('从本项目视频库选择') as HTMLSelectElement;
  expect(Array.from(projectPicker.options).map(option => option.value)).toEqual(['']);
  await user.click(screen.getByRole('button', { name: '读取已有视频素材库' }));
  const globalPicker = await screen.findByLabelText('已有素材库视频');
  await user.selectOptions(globalPicker, video.id);
  expect(Array.from(projectPicker.options).map(option => option.value)).toEqual(['']);
  expect(mocked.mock.calls.some(([url, init]) =>
    url === '/api/studio/projects/p1/reference-videos/ref-video-1/attach' && init?.method === 'POST',
  )).toBe(false);

  await user.click(screen.getByRole('button', { name: '添加到当前项目' }));
  await waitFor(() => expect(mocked.mock.calls.some(([url, init]) =>
    url === '/api/studio/projects/p1/reference-videos/ref-video-1/attach' && init?.method === 'POST',
  )).toBe(true));
  await waitFor(() => expect(Array.from(projectPicker.options).map(option => option.value)).toContain(video.id));
});
