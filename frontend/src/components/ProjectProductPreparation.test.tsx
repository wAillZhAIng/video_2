// UI contracts; real photo storage and generator inputs are exercised by backend tests.
import {cleanup,render,screen,waitFor} from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {afterEach,expect,it,vi} from 'vitest';
import {request} from '../lib/api';
import {ProjectProductPreparation} from './ProjectProductPreparation';
vi.mock('../lib/api',async original=>({...await original<typeof import('../lib/api')>(),request:vi.fn()}));
const mocked=vi.mocked(request);
const product={id:'product1',name:'固定礼盒',state:'active',revision:2,images:[{id:'im1',filename:'real.jpg',thumbnail_url:'/real.jpg'},{id:'im2',filename:'front.png',thumbnail_url:'/front.png'}]};
function install(){let data={project_id:'p1',product,expected_token:'a'.repeat(64),image_ids:[] as string[],shot_ids:[] as string[],bound:false,shots:[{id:'s1',order:1,title:'固定礼盒特写',suggested:true},{id:'s2',order:2,title:'人物离开',suggested:false}]};mocked.mockImplementation(async(url,init)=>{
  if(url==='/api/product-library'&&init?.method==='POST')return product;
  if(url==='/api/product-library?include_archived=true')return {items:[product],limits:{}};
  if(url.includes('/references?'))return structuredClone(data);
  if(init?.method==='PUT'){const body=JSON.parse(String(init.body));data={...data,...body,bound:true};return structuredClone(data);}
  if(url.endsWith('/images'))return {product,added:product.images,duplicates:[],errors:[]};
  throw new Error('Unexpected request '+url);
});}
afterEach(()=>{cleanup();mocked.mockReset();vi.restoreAllMocks();});
it('uses the server Context-IR limit and submits every one of eight selected views',async()=>{
  const images=Array.from({length:8},(_,index)=>({id:`view-${index}`,filename:`view-${index}.png`,thumbnail_url:`/view-${index}.png`}));
  const item={...product,images};
  let selected={project_id:'p1',product:item,expected_token:'b'.repeat(64),image_ids:[] as string[],shot_ids:[] as string[],bound:false,
    shots:[{id:'s1',order:1,title:'固定礼盒特写',suggested:true}],note:'',limits:{max_reference_images:8}};
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/product-library?include_archived=true')return {items:[item],limits:{max_generation_images:8}};
    if(init?.method==='PUT'){selected={...selected,...JSON.parse(String(init.body)),bound:true};return structuredClone(selected);}
    if(url.includes('/references?'))return structuredClone(selected);
    throw new Error('Unexpected '+url);
  });
  const user=userEvent.setup();render(<ProjectProductPreparation projectId="p1" shots={[]} disabled={false}/>);
  await user.click(screen.getByText('固定产品／道具参考图（可选）'));await screen.findAllByRole('option',{name:'固定礼盒'});
  await user.selectOptions(screen.getByLabelText('固定产品／道具'),'product1');
  await screen.findByLabelText('固定礼盒参考图 8');
  for(let index=1;index<=8;index++)await user.click(screen.getByLabelText(`固定礼盒参考图 ${index}`));
  await user.click(screen.getByRole('button',{name:'按剧本同名内容选择分镜'}));
  await user.click(screen.getByRole('button',{name:'保存产品／道具项目引用'}));
  await waitFor(()=>expect(mocked.mock.calls.some(([,init])=>init?.method==='PUT')).toBe(true));
  const body=JSON.parse(String(mocked.mock.calls.find(([,init])=>init?.method==='PUT')![1]!.body));
  expect(body.image_ids).toEqual(images.map(image=>image.id));
  const headers=new Headers(mocked.mock.calls.find(([,init])=>init?.method==='PUT')![1]!.headers);
  expect(headers.get('X-Reference-Image-Count')).toBe('8');
  expect(headers.get('X-Reference-Trace-ID')).toMatch(/^[a-f0-9-]{36}$/);
});
it('is optional without fetching or creating references while collapsed',()=>{const states:unknown[]=[];render(<ProjectProductPreparation projectId="p1" shots={[]} disabled={false} onStateChange={state=>states.push(state)}/>);expect(screen.getByText('固定产品／道具参考图（可选）')).toBeInTheDocument();expect(mocked).not.toHaveBeenCalled();expect(states.at(-1)).toEqual({busy:false,dirty:false,overBudget:false});});
it('saves zero product images for the selected shot and keeps its editor open',async()=>{
  const shots=[{id:'s1',order:1,title:'首镜'},{id:'s2',order:2,title:'次镜'}];
  const states:unknown[]=[];
  const rows=Object.fromEntries(shots.map(shot=>[shot.id,{product_id:product.id,name:product.name,revision:2,
    images:product.images,video_image_ids:['im1'] as string[],keyframe_image_ids:['im1'] as string[]} ]));
  mocked.mockImplementation(async(url,init)=>{
    if(url==='/api/product-library?include_archived=true')return {items:[product],limits:{}};
    const shotId=url.includes('/shots/s2/')?'s2':'s1';
    if(init?.method==='PUT'){
      const {video_image_ids,keyframe_image_ids}=JSON.parse(String(init.body)).products[0];
      rows[shotId]={...rows[shotId],revision:3,video_image_ids,keyframe_image_ids};
    }
    return {project_id:'p1',shot_id:shotId,products:[structuredClone(rows[shotId])],
      limits:{keyframe_total_max:4,video_total_max:9},note:''};
  });
  const user=userEvent.setup();
  render(<ProjectProductPreparation projectId="p1" shots={shots} disabled={false} onStateChange={state=>states.push(state)}/>);
  await user.click(screen.getByText('固定产品／道具参考图（可选）'));
  await user.click(screen.getByText('产品图的制作引用（逐镜可选）'));
  await user.selectOptions(screen.getByLabelText('选择产品参考分镜'),'s2');
  expect(await screen.findByLabelText('固定礼盒图 1 用于视频')).toBeChecked();
  await user.click(screen.getByRole('button',{name:'清空本镜产品引用'}));
  await user.click(screen.getByRole('button',{name:'保存此镜头的产品参考选择'}));
  expect(await screen.findByText('此分镜的产品参考已保存。')).toBeVisible();
  expect(screen.getByLabelText('选择产品参考分镜')).toHaveValue('s2');
  expect(screen.getByLabelText('固定礼盒图 1 用于关键画面')).not.toBeChecked();
  expect(screen.getByLabelText('固定礼盒图 1 用于视频')).not.toBeChecked();
  const saved=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(saved[0]).toBe('/api/product-library/projects/p1/shots/s2/references');
  expect(JSON.parse(String(saved[1]?.body))).toEqual({products:[{product_id:'product1',expected_revision:2,
    keyframe_image_ids:[],video_image_ids:[]}]});
  await waitFor(()=>expect(states.at(-1)).toEqual({busy:false,dirty:false,overBudget:false}));
  await user.selectOptions(screen.getByLabelText('选择产品参考分镜'),'s1');
  expect(await screen.findByLabelText('固定礼盒图 1 用于视频')).toBeChecked();
});
it('selects real library image IDs for only explicit product shots and reloads persistence',async()=>{
  install();const user=userEvent.setup();render(<ProjectProductPreparation projectId="p1" shots={[]} disabled={false}/>);
  await user.click(screen.getByText('固定产品／道具参考图（可选）'));await screen.findAllByRole('option',{name:'固定礼盒'});
  await user.selectOptions(screen.getByLabelText('固定产品／道具'),'product1');await screen.findByLabelText('固定礼盒参考图 1');
  await user.click(screen.getByLabelText('固定礼盒参考图 1'));await user.click(screen.getByRole('button',{name:'按剧本同名内容选择分镜'}));
  expect(screen.getByLabelText('#1 · 固定礼盒特写 · 同名匹配')).toBeChecked();expect(screen.getByLabelText('#2 · 人物离开')).not.toBeChecked();
  await user.click(screen.getByRole('button',{name:'保存产品／道具项目引用'}));await screen.findByText(/已保存：1 个分镜/);
  const put=mocked.mock.calls.find(([,init])=>init?.method==='PUT')!;
  expect(JSON.parse(String(put[1]?.body))).toEqual({product_id:'product1',expected_token:'a'.repeat(64),image_ids:['im1'],shot_ids:['s1']});
  await waitFor(()=>expect(screen.getByLabelText('固定礼盒参考图 1')).toBeChecked());
});
it('bulk uploads to a named library product without dispatching production',async()=>{
  install();const user=userEvent.setup();render(<ProjectProductPreparation projectId="p1" shots={[]} disabled={false}/>);
  await user.click(screen.getByText('固定产品／道具参考图（可选）'));await user.type(screen.getByLabelText('产品／道具名称'),'固定礼盒');
  const files=[new File(['jpeg-original'],'real.jpg',{type:'image/jpeg'}),new File(['png-original'],'front.png',{type:'image/png'})];
  await user.upload(screen.getByLabelText('批量选择产品／道具图'),files);await user.click(screen.getByRole('button',{name:'上传并保存到图库'}));
  await screen.findByText(/服务器已确认保存 2 张/);const sent=mocked.mock.calls.find(([url])=>url==='/api/product-library/product1/images')!;
  expect((sent[1]?.body as FormData).getAll('files')).toEqual(files);
  expect(mocked.mock.calls.some(([url])=>url.includes('/start')||url.includes('/generate'))).toBe(false);
  expect(await screen.findByLabelText('固定礼盒参考图 1')).not.toBeChecked();
});
it('keeps draft selections when refresh is declined and shows save failures',async()=>{
  install();vi.spyOn(window,'confirm').mockReturnValue(false);const user=userEvent.setup();render(<ProjectProductPreparation projectId="p1" shots={[]} disabled={false}/>);
  await user.click(screen.getByText('固定产品／道具参考图（可选）'));await screen.findAllByRole('option',{name:'固定礼盒'});await user.selectOptions(screen.getByLabelText('固定产品／道具'),'product1');await screen.findByLabelText('固定礼盒参考图 1');await user.click(screen.getByLabelText('固定礼盒参考图 1'));
  await user.click(screen.getByRole('button',{name:'刷新图库与项目引用'}));expect(screen.getByLabelText('固定礼盒参考图 1')).toBeChecked();
  const base=mocked.getMockImplementation()!;mocked.mockImplementation(async(url,init)=>{if(init?.method==='PUT')throw new Error('引用已变化，请刷新');return base(url,init);});await user.click(screen.getByRole('button',{name:'保存产品／道具项目引用'}));expect(await screen.findByRole('alert')).toHaveTextContent('引用已变化');expect(screen.getByLabelText('固定礼盒参考图 1')).toBeChecked();
});
