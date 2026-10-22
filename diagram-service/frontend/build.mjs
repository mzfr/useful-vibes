import {build} from 'vite';
import fs from 'node:fs';
import path from 'node:path';
process.chdir(import.meta.dirname);
await build();
fs.cpSync('node_modules/@excalidraw/excalidraw/dist/prod/fonts','dist/fonts',{recursive:true});
const lock=JSON.parse(fs.readFileSync('package-lock.json','utf8'));
for(const location of Object.keys(lock.packages)) {
 if(!location || !fs.existsSync(location))continue;
 for(const name of fs.readdirSync(location).filter(name=>/^(licen[sc]e|copying|notice)(\.|$|-)/i.test(name))) {
  const dest=path.join('dist/licenses',location,name);
  fs.mkdirSync(path.dirname(dest),{recursive:true});
  fs.cpSync(path.join(location,name),dest,{recursive:true});
 }
}
