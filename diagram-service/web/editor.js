import {createDrawio} from './drawio.js';
import {request,setupTheme,download} from './shared.js';
const params=new URLSearchParams(location.search), rendering=params.get('render')==='1';
const id=location.pathname.split('/').pop(), endpoint=`/api/diagrams/${id}/source`;
const container=document.querySelector('#canvas'), status=document.querySelector('#status');
let adapter,meta,revision,saved,latest,busy=false,conflict=false,loading=false,loaded=false,timer;
const theme=setupTheme(async value=>{try{await adapter?.setTheme(value);}catch(e){report(e.message,true);}});
function report(text,error=false){status.textContent=text;status.classList.toggle('error',error);}
function changed(source){if(loading||!loaded||rendering)return;latest=source;if(latest!==saved){report('Unsaved changes…');clearTimeout(timer);timer=setTimeout(tick,350);}}
async function makeAdapter(type,theme) {
 if(type==='drawio')return createDrawio(container,{theme,onChange:changed});
 window.EXCALIDRAW_ASSET_PATH=location.origin+'/excalidraw/';
 const link=document.createElement('link');link.rel='stylesheet';link.href='/excalidraw/adapter.css';document.head.append(link);
 await import('/excalidraw/adapter.js');
 return window.createExcalidraw(container,{theme,onChange:changed});
}
// Render only the immutable source supplied by Go; never fetch a changing source here.
window.renderDiagram=async(source,type,exportTheme,page)=>{
 adapter=await makeAdapter(type,exportTheme);await adapter.load(source);return adapter.exportPNG(exportTheme,page);
};
async function apply(response,force=false){
 const source=await response.text();
 // Reading the body yields: a user may have edited since the polling GET began.
 if(loaded&&latest!==saved&&!force)return;
 loading=true;container.inert=true;
 try {await adapter.load(source);latest=saved=adapter.getSource();revision=response.headers.get('ETag');loaded=true;conflict=false;report('Saved · watching for changes');}
 finally{loading=false;container.inert=false;}
}
async function tick(){
 if(busy||conflict||!loaded)return;busy=true;
 try {
  if(latest!==saved){const snapshot=latest;report('Saving…');const result=await(await request(endpoint,{method:'PUT',headers:{'If-Match':revision,'Content-Type':meta.type==='drawio'?'application/xml':'application/json'},body:snapshot})).json();revision=`"${result.revision}"`;saved=snapshot;report(latest===saved?'Saved · watching for changes':'Unsaved changes…');}
  else {const response=await request(endpoint);if(response.headers.get('ETag')!==revision&&latest===saved)await apply(response);}
 }catch(e){if(e.status===409){conflict=true;report('Conflict: your edits are kept. Download source before loading the server version.',true);}else report(`Sync failed; will retry: ${e.message}`,true);}
 finally{busy=false;}
}
if(rendering){document.querySelector('header').hidden=true;}
else {
 window.addEventListener('beforeunload',e=>{if(loaded&&latest!==saved){e.preventDefault();e.returnValue='';}});
 document.querySelector('#raw').onclick=()=>{if(adapter&&meta)download(adapter.getSource(),meta.name.replace(/\.(excalidraw|drawio)$/i,'')+'.'+meta.type);};
 document.querySelector('#reload').onclick=async()=>{if(busy)return;if(latest!==saved&&!confirm('Discard your unsaved edits and load the server version? Download source first to keep them.'))return;busy=true;try{await apply(await request(endpoint),true);}catch(e){report(e.message,true);}finally{busy=false;}};
 document.querySelector('#png').onclick=async event=>{
  if(!adapter)return;
  event.target.disabled=true;
  let scratch,exporter=adapter;
  try {
   const exportTheme=document.querySelector('#export-theme').value;
   // A separate draw.io frame avoids reloading the live editor to export another theme/page.
   if(meta.type==='drawio'){
    const snapshot=adapter.getSource();
    scratch=document.createElement('div');scratch.style.cssText='position:fixed;left:-20000px;width:1200px;height:800px';document.body.append(scratch);
    exporter=await createDrawio(scratch,{theme:exportTheme});await exporter.load(snapshot);
   }
   download(await exporter.exportPNG(exportTheme,Number(document.querySelector('#page').value)-1),meta.name+'.png');
  }catch(e){report('Export failed: '+e.message,true);}
  finally{if(scratch){if(exporter!==adapter)exporter.destroy();scratch.remove();}event.target.disabled=false;}
 };
 try {meta=await(await request(`/api/diagrams/${id}`)).json();document.querySelector('#name').textContent=meta.name;document.title=meta.name;document.querySelector('#page-label').hidden=meta.type!=='drawio';adapter=await makeAdapter(meta.type,theme);await apply(await request(endpoint));setInterval(tick,1000);}catch(e){report('Load failed: '+e.message,true);}
}
