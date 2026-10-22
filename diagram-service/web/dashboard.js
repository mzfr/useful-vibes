import {request, setupTheme} from './shared.js';
setupTheme();
let diagrams=[];
const status=document.querySelector('#status');
function render() {
  const q=document.querySelector('#search').value.toLowerCase();
  const list=document.querySelector('#list'); list.replaceChildren();
  for(const d of diagrams.filter(d=>(d.name+' '+d.type).toLowerCase().includes(q))) {
    const a=document.createElement('a'); a.className='card'; a.href='/d/'+d.id;
    const title=document.createElement('strong'); title.textContent=d.name;
    const sub=document.createElement('small'); sub.textContent=`${d.type} · ${new Date(d.updatedAt).toLocaleString()}`;
    a.append(title,sub); list.append(a);
  }
  status.textContent=`${list.children.length} diagrams`;
}
async function refresh() {try {diagrams=await (await request('/api/diagrams')).json();render();}catch(e){status.textContent=e.message;}}
async function create(type,name,source) {
  try {const d=await (await request('/api/diagrams',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({type,name,source})})).json(); location.href='/d/'+d.id;}catch(e){status.textContent=e.message;}
}
document.querySelector('#search').oninput=render;
document.querySelector('#refresh').onclick=refresh;
for(const type of ['excalidraw','drawio']) document.querySelector('#new-'+type).onclick=()=>{
  const name=prompt('Diagram name');if(!name?.trim())return;
  create(type,name,type==='excalidraw'?JSON.stringify({type:'excalidraw',version:2,elements:[],appState:{},files:{}}):'<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>');
};
document.querySelector('#upload').onchange=async event=>{
  try {const file=event.target.files[0];if(!file)return;if(file.size>16*1024*1024)throw Error('File exceeds 16 MiB');
    const source=await file.text();let type;
    try {if(JSON.parse(source).type==='excalidraw')type='excalidraw';}catch{}
    if(!type){const xml=new DOMParser().parseFromString(source,'application/xml');if(!xml.querySelector('parsererror')&&['mxfile','mxGraphModel'].includes(xml.documentElement.localName))type='drawio';}
    if(!type)throw Error('Expected native Excalidraw JSON or draw.io XML');
    await create(type,file.name,source);
  }catch(e){status.textContent=e.message;}
};
refresh();
