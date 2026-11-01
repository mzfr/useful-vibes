const TIMEOUT_MS = 30_000;
const MAX_PNG_EDGE = 8192;
const MAX_PNG_PIXELS = 16_000_000;
const EMPTY_DIAGRAM = '<mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/></root></mxGraphModel>';

function assertTheme(theme) {
	if (theme !== 'light' && theme !== 'dark') {
		throw new TypeError('theme must be "light" or "dark"');
	}
}

function assertPage(page) {
	if (!Number.isSafeInteger(page) || page < 0) {
		throw new TypeError('page must be a non-negative integer');
	}
}

function pageIdFor(source, page) {
	const document = new DOMParser().parseFromString(source, 'application/xml');

	if (document.querySelector('parsererror')) {
		throw new Error('Cannot select a page from invalid draw.io XML');
	}

	if (document.documentElement.localName !== 'mxfile') {
		if (page !== 0) {
			throw new RangeError('A single-page draw.io diagram only has page 0');
		}

		return null;
	}

	const pages = Array.from(document.documentElement.children)
		.filter((element) => element.localName === 'diagram');
	const selected = pages[page];

	if (selected == null) {
		throw new RangeError(`Draw.io page ${page} does not exist`);
	}

	const id = selected.getAttribute('id');
	if (!id) {
		throw new Error(`Draw.io page ${page} has no id`);
	}

	return id;
}

function exportScale(bounds) {
	const width = Math.ceil(Number(bounds?.width));
	const height = Math.ceil(Number(bounds?.height));

	if (!Number.isFinite(width) || !Number.isFinite(height) || width < 0 || height < 0) {
		throw new Error('Draw.io did not provide export bounds');
	}

	// A horizontal or vertical edge can have a zero graph-bound dimension.
	// Canvas still allocates at least one pixel for that dimension.
	const exportWidth = Math.max(1, width);
	const exportHeight = Math.max(1, height);
	let scale = Math.min(3, MAX_PNG_EDGE / exportWidth, MAX_PNG_EDGE / exportHeight,
		Math.sqrt(MAX_PNG_PIXELS / (exportWidth * exportHeight)));

	while (Math.ceil(exportWidth * scale) > MAX_PNG_EDGE ||
		Math.ceil(exportHeight * scale) > MAX_PNG_EDGE ||
		Math.ceil(exportWidth * scale) * Math.ceil(exportHeight * scale) > MAX_PNG_PIXELS) {
		scale *= 0.999;
	}

	if (!Number.isFinite(scale) || scale <= 0) {
		throw new Error('Draw.io diagram is too large to export safely');
	}

	return scale;
}

function assertPngSize(dataUrl) {
	const prefix = 'data:image/png;base64,';
	if (typeof dataUrl !== 'string' || !dataUrl.startsWith(prefix)) {
		throw new Error('Draw.io did not return a PNG data URL');
	}

	const header = atob(dataUrl.slice(prefix.length, prefix.length + 44));
	if (header.length < 24 || header.slice(1, 4) !== 'PNG') {
		throw new Error('Draw.io returned an invalid PNG');
	}

	const width = (header.charCodeAt(16) * 2 ** 24) + (header.charCodeAt(17) * 2 ** 16) +
		(header.charCodeAt(18) * 2 ** 8) + header.charCodeAt(19);
	const height = (header.charCodeAt(20) * 2 ** 24) + (header.charCodeAt(21) * 2 ** 16) +
		(header.charCodeAt(22) * 2 ** 8) + header.charCodeAt(23);

	if (width > MAX_PNG_EDGE || height > MAX_PNG_EDGE || width * height > MAX_PNG_PIXELS) {
		throw new Error('Draw.io PNG exceeds the 8192px/16MP export limit');
	}
}

/**
 * Creates a same-origin draw.io embed adapter.
 *
 * The JSON embed protocol sends {event:'init'}, accepts {action:'load'} and
 * {action:'export', format:'png'}, then returns load/autosave/export events.
 */
export async function createDrawio(container, {theme = 'light', onChange} = {}) {
	assertTheme(theme);
	if (!(container instanceof HTMLElement)) {
		throw new TypeError('container must be an HTMLElement');
	}
	if (onChange != null && typeof onChange !== 'function') {
		throw new TypeError('onChange must be a function');
	}

	const origin = window.location.origin;
	const iframe = document.createElement('iframe');
	iframe.title = 'Draw.io editor';
	iframe.style.border = '0';
	iframe.style.display = 'block';
	iframe.style.width = '100%';
	iframe.style.height = '100%';
	iframe.src = `/drawio/index.html?embed=1&proto=json&offline=1&autosave=1&pwa=0&plugins=0&dark=${theme === 'dark' ? '1' : '0'}`;

	let source = EMPTY_DIAGRAM;
	let editorTheme = theme;
	let bounds = null;
	let initialized = false;
	let fatalError = null;
	let pending = null;
	let requestId = 0;
	let queue = Promise.resolve();
	let resolveInit;
	let rejectInit;
	const init = new Promise((resolve, reject) => {
		resolveInit = resolve;
		rejectInit = reject;
	});
	const initTimer = window.setTimeout(() => {
		fail(new Error('Draw.io initialization timed out after 30 seconds'));
	}, TIMEOUT_MS);

	function clearPending() {
		if (pending != null) {
			window.clearTimeout(pending.timer);
			pending = null;
		}
	}

	function fail(error) {
		if (fatalError != null) return;
		fatalError = error instanceof Error ? error : new Error(String(error));
		window.clearTimeout(initTimer);
		const current = pending;
		clearPending();
		if (current != null) current.reject(fatalError);
		rejectInit(fatalError);
	}

	function finishPending(error, value) {
		const current = pending;
		if (current == null) return;
		window.clearTimeout(current.timer);
		pending = null;
		if (error != null) current.reject(error);
		else current.resolve(value);
	}

	function post(message) {
		if (fatalError != null) throw fatalError;
		const target = iframe.contentWindow;
		if (target == null) throw new Error('Draw.io frame is unavailable');
		target.postMessage(JSON.stringify(message), origin);
	}

	function request(event, message) {
		if (pending != null) {
			return Promise.reject(new Error('Draw.io protocol request already pending'));
		}

		return new Promise((resolve, reject) => {
			const id = ++requestId;
			pending = {
				event,
				id,
				resolve,
				reject,
				timer: window.setTimeout(() => {
					if (pending?.id === id) {
						finishPending(new Error(`Draw.io ${event} timed out after 30 seconds`));
					}
				}, TIMEOUT_MS),
			};
			try {
				post({...message, requestId: id});
			}
			catch (error) {
				finishPending(error);
			}
		});
	}

	function updateBounds(message) {
		if (message?.modelBounds != null) {
			bounds = message.modelBounds;
		}
	}

	function handleMessage(event) {
		if (event.origin !== origin || event.source !== iframe.contentWindow || typeof event.data !== 'string') {
			return;
		}

		let message;
		try {
			message = JSON.parse(event.data);
		}
		catch {
			return;
		}
		if (message == null || typeof message !== 'object' || Array.isArray(message)) return;

		if (message.event === 'init') {
			if (!initialized) {
				initialized = true;
				window.clearTimeout(initTimer);
				resolveInit();
			}
			return;
		}

		if (typeof message.error === 'string' && pending != null) {
			finishPending(new Error(`Draw.io protocol error: ${message.error}`));
			return;
		}

		if (message.event === 'autosave' && typeof message.xml === 'string') {
			updateBounds(message);
			source = message.xml;
			if (onChange != null) onChange(source);
			return;
		}

		if (message.event === 'load' && pending?.event === 'load') {
			updateBounds(message);
			finishPending(null, message);
			return;
		}

		if (message.event === 'export' && pending?.event === 'export' &&
			message.message?.requestId === pending.id) {
			updateBounds(message);
			finishPending(null, message);
		}
	}

	function enqueue(task) {
		const result = queue.then(task, task);
		queue = result.catch(() => {});
		return result;
	}

	async function loadNow(nextSource, nextTheme) {
		if (typeof nextSource !== 'string') {
			throw new TypeError('sourceString must be a string');
		}
		assertTheme(nextTheme);
		await request('load', {
			action: 'load',
			xml: nextSource,
			autosave: 1,
			exportProtocol: true,
			dark: nextTheme === 'dark',
		});
		source = nextSource;
		editorTheme = nextTheme;
	}

	iframe.addEventListener('error', () => fail(new Error('Unable to load /drawio/index.html')));
	window.addEventListener('message', handleMessage);
	container.appendChild(iframe);

	try {
		await init;
		await loadNow(source, editorTheme);
	}
	catch (error) {
		window.removeEventListener('message', handleMessage);
		throw error;
	}

	return {
		destroy() {
			window.removeEventListener('message', handleMessage);
			window.clearTimeout(initTimer);
			clearPending();
			iframe.remove();
		},
		load(nextSource) {
			return enqueue(() => loadNow(nextSource, editorTheme));
		},
		setTheme(nextTheme) {
			return enqueue(() => loadNow(source, nextTheme));
		},
		exportPNG(nextTheme, page = 0) {
			return enqueue(async () => {
				assertTheme(nextTheme);
				assertPage(page);
				const previousTheme = editorTheme;
				if (previousTheme !== nextTheme) {
					await loadNow(source, nextTheme);
				}

				try {
					const pageId = pageIdFor(source, page);
					const response = await request('export', {
						action: 'export',
						format: 'png',
						pageId,
						keepTheme: true,
						border: 0,
						scale: exportScale(bounds),
					});
					assertPngSize(response.data);
					return response.data;
				}
				finally {
					if (previousTheme !== nextTheme) {
						await loadNow(source, previousTheme);
					}
				}
			});
		},
		getSource() {
			return source;
		},
	};
}
