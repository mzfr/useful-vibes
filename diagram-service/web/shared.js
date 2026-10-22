export async function request(url,options={}) {
 const response=await fetch(url,{cache:'no-store',signal:AbortSignal.timeout(15000),...options});
 if(!response.ok){let text;try{text=(await response.json()).error;}catch{}const error=Error(text||`HTTP ${response.status}`);error.status=response.status;throw error;}return response;
}
export function setupTheme(onChange=()=>{}) {
 const select=document.querySelector('#theme');
 const initial=new URLSearchParams(location.search).get('theme')||localStorage.getItem('diagram-theme')||'light';
 select.value=initial==='dark'?'dark':'light';document.documentElement.dataset.theme=select.value;
 select.onchange=async()=>{document.documentElement.dataset.theme=select.value;localStorage.setItem('diagram-theme',select.value);await onChange(select.value);};return select.value;
}
export function download(data,name,type='application/octet-stream') {
 const url=typeof data==='string'&&data.startsWith('data:')?data:URL.createObjectURL(new Blob([data],{type}));
 const a=document.createElement('a');a.href=url;a.download=name;a.click();if(url.startsWith('blob:'))setTimeout(()=>URL.revokeObjectURL(url),60000);
}
