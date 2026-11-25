// UI/controller regression only: controlled HTTP results, no real model or media execution.
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, useLocation, useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import App from '../App';
import { api, ApiError, request } from '../lib/api';
import type { Project, StoryboardShot } from '../lib/types';
import type { StudioOverview } from '../lib/studio-contract';
import { ToastProvider } from '../components/ToastProvider';

vi.mock('../lib/api', async original => ({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
vi.mock('../components/ShotWorkspace', () => ({ ShotWorkspace: () => null }));
const mocked=vi.mocked(request);
const projects=[{id:'prj_new',name:'最新项目'},{id:'prj_older',name:'较早项目'}] as Project[];
function overview(id:string, completed=false):StudioOverview {
  return {project:projects.find(project=>project.id===id)!,shots:[],run:{id:`run-${id}`,status:completed?'completed':'rendering',
    result_json:{render_id:`render-${id}`}},renders:[{id:`render-${id}`,status:completed?'generated':'queued',
      relative_path:completed?`${id}.mp4`:null},{id:'historical',status:'generated',relative_path:'historical.mp4'}]};
}
const shot=(id:string):StoryboardShot=>({id:`${id}-shot`,projectId:id,order:1,sceneTitle:`${id} 分镜`,status:'draft',revision:1,
  aspectRatio:'9:16',durationSeconds:5,visualPrompt:'测试描述',narration:'',dialogue:'',negativePrompt:'',action:'',cameraMove:'',location:'',lighting:'',audioDirection:''} as StoryboardShot);
function Probe(){const location=useLocation(),navigate=useNavigate();return <><output aria-label="当前地址">{location.pathname}{location.search}{location.hash}</output><button onClick={()=>navigate(-1)}>浏览器后退</button><button onClick={()=>navigate(1)}>浏览器前进</button></>;}
function mount(url:string,entries=[url]){return render(<MemoryRouter initialEntries={entries} initialIndex={0}><ToastProvider><App/><Probe/></ToastProvider></MemoryRouter>);}
beforeEach(()=>{
  vi.spyOn(api,'getContextProviderHealth').mockResolvedValue({ok:true,connected:true,configured:true,provider:'comfyui_h3',mode:'real'});
  vi.spyOn(api,'listProjects').mockResolvedValue(projects);
  vi.spyOn(api,'getScript').mockRejectedValue(new ApiError('尚无剧本记录',404));
  vi.spyOn(api,'listShots').mockImplementation(async id=>[shot(id)]);
  vi.spyOn(api,'listCharacters').mockResolvedValue([]);
  mocked.mockImplementation(async url=>{
    if(url==='/api/studio/projects')return projects;
    const project=url.match(/^\/api\/studio\/projects\/(prj_[^/]+)$/)?.[1];
    if(project)return overview(project,true);
    throw new Error(`Unexpected non-model request ${url}`);
  });
});
afterEach(()=>{cleanup();mocked.mockReset();});

it('keeps picker, refresh and sidebar on the selected older project',async()=>{
  const path='/storyboard';
  const user=userEvent.setup();
  const fixture=mount(`${path}?project=prj_older&shot=old-shot&video=old-video&job=old-job&quality_render=old-render&qualityrender=old-render-2&view=compact`);
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  const reads=vi.mocked(api.listShots);
  await waitFor(()=>expect(reads.mock.calls.filter(([id])=>id==='prj_older')).toHaveLength(1));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  const changed=new URL(screen.getByLabelText('当前地址').textContent!,'http://localhost');
  expect(changed.searchParams.get('project')).toBe('prj_new');
  for(const key of ['shot','video','job','quality_render','qualityrender'])expect(changed.searchParams.has(key)).toBe(false);
  expect(changed.searchParams.get('view')).toBe('compact');
  await waitFor(()=>expect(reads.mock.calls.filter(([id])=>id==='prj_new')).toHaveLength(1));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_older');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  await waitFor(()=>expect(reads.mock.calls.filter(([id])=>id==='prj_older')).toHaveLength(2));
  for(const label of ['脚本与分镜','项目工作台']){
    const nav=within(screen.getByRole('navigation',{name:'主导航'}));
    expect(nav.getByRole('link',{name:label})).toHaveAttribute('href',expect.stringContaining('project=prj_older'));
    await user.click(nav.getByRole('link',{name:label}));
    await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  }
  const refreshUrl=screen.getByLabelText('当前地址').textContent!;
  fixture.unmount();
  mount(refreshUrl);
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  expect(screen.getByLabelText('当前地址')).toHaveTextContent('/?project=prj_older');
});

it('does not restore an older completed output after the picker changes URL project',async()=>{
  let finish!:(data:StudioOverview)=>void;
  const old=new Promise<StudioOverview>(resolve=>{finish=resolve;});
  const fallback=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>url==='/api/studio/projects/prj_older'?old:fallback(url,init));
  const user=userEvent.setup();
  mount('/review?project=prj_older');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older'));
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toBeEnabled());
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  expect(await screen.findByLabelText('本次自动成片')).toHaveAttribute('src',expect.stringContaining('prj_new.mp4'));
  await act(async()=>{finish(overview('prj_older',true));await old;});
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new');
  expect(screen.getByLabelText('本次自动成片')).toHaveAttribute('src',expect.stringContaining('prj_new.mp4'));
  expect(within(screen.getByRole('navigation',{name:'主导航'})).getByRole('link',{name:'项目工作台'})).toHaveAttribute('href','/?project=prj_new');
});

it('does not restore older Storyboard details when a later selected project finishes first',async()=>{
  let finish!:(shots:StoryboardShot[])=>void;
  const old=new Promise<StoryboardShot[]>(resolve=>{finish=resolve;});
  vi.mocked(api.listShots).mockImplementation(async id=>id==='prj_new'?old:[shot(id)]);
  const user=userEvent.setup();mount('/storyboard?project=prj_older');
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_new');
  await waitFor(()=>expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new'));
  await user.selectOptions(screen.getByLabelText('当前项目'),'prj_older');
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  await act(async()=>{finish([shot('prj_new')]);await old;});
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older');
  expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜');
  expect(vi.mocked(api.listShots).mock.calls.filter(([id])=>id==='prj_new')).toHaveLength(1);
});

it('responds to browser forward and back project URLs without remounting Storyboard',async()=>{
  const old='/storyboard?project=prj_older',next='/storyboard?project=prj_new';
  const user=userEvent.setup();mount(old,[old,next]);
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  await user.click(screen.getByRole('button',{name:'浏览器前进'}));
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_new 分镜'));
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_new');
  await user.click(screen.getByRole('button',{name:'浏览器后退'}));
  await waitFor(()=>expect(screen.getByLabelText('场景标题')).toHaveValue('prj_older 分镜'));
  expect(screen.getByLabelText('当前项目')).toHaveValue('prj_older');
});

it('polls the current automatic render and never uses an older successful file as its output',async()=>{
  let rendered=false;
  const fallback=mocked.getMockImplementation()!;
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/studio/projects/prj_older')return overview('prj_older',rendered);
    return fallback(url,init);
  });
  mount('/final?project=prj_older');
  await screen.findByText('正在合成成片。');
  expect(screen.queryByLabelText('本次自动成片')).not.toBeInTheDocument();
  rendered=true;
  await waitFor(()=>expect(screen.getByLabelText('本次自动成片')).toHaveAttribute('src','/api/media/prj_older.mp4'),{timeout:5500});
});
