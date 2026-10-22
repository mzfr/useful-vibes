import React from 'react';
import { createRoot } from 'react-dom/client';
import { Excalidraw, restore, serializeAsJSON, exportToBlob } from '@excalidraw/excalidraw';
import '@excalidraw/excalidraw/index.css';

const stable = value => JSON.stringify(value, (_, v) => v && typeof v === 'object' && !Array.isArray(v) ? Object.fromEntries(Object.keys(v).sort().map(k => [k, v[k]])) : v);
const serialize = (elements, state, files) => stable(JSON.parse(serializeAsJSON(elements, state, files, 'local')));
window.createExcalidraw = async function(container, {theme, onChange}) {
  let api, current = '', key = 0;
  const root = createRoot(container);
  async function load(source) {
    const scene = restore(JSON.parse(source), null, null, {repairBindings: true});
    current = serialize(scene.elements, scene.appState, scene.files);
    const viewport = api?.getAppState();
    scene.appState = {...scene.appState, theme, ...(viewport ? {scrollX:viewport.scrollX, scrollY:viewport.scrollY, zoom:viewport.zoom} : {})};
    if (!viewport) scene.scrollToContent = true;
    const generation = ++key;
    await new Promise(resolve => root.render(<Excalidraw key={generation} initialData={scene}
      excalidrawAPI={value => { api = value; resolve(); }}
      onChange={(elements, state, files) => {
        if (state.isLoading || generation !== key) return;
        const next = serialize(elements, state, files);
        if (next !== current) { current = next; onChange?.(next); }
      }} UIOptions={{canvasActions:{saveToActiveFile:false}}} />));
  }
  return {load, getSource: () => current,
    async setTheme(value) { theme = value; api?.updateScene({appState:{theme}}); },
    async exportPNG(value, page=0) {
      if (page !== 0) throw Error('Excalidraw only supports page 0');
      await document.fonts.ready;
      const blob = await exportToBlob({elements:api.getSceneElements(), files:api.getFiles(),
        appState:{...api.getAppState(), exportBackground:true, exportWithDarkMode:value === 'dark'},
        maxWidthOrHeight:4000});
      return await new Promise((resolve,reject) => { const reader = new FileReader(); reader.onload=()=>resolve(reader.result); reader.onerror=()=>reject(reader.error); reader.readAsDataURL(blob); });
    }
  };
};
window.dispatchEvent(new Event('adapter-ready'));
