const SHARE_COOKIE = "__Host-intake_share";
const VISIT_COOKIE = "__Host-intake_visit";
const VISIT_WINDOW_SECONDS = 30 * 60;
const YEAR_SECONDS = 365 * 24 * 60 * 60;
const TOKEN_PATH = /^\/s\/([A-Za-z0-9_-]{32,64})\/?$/;

const POSTER_PATH = /^\/posters\/[a-f0-9]{20}\.(jpg|png|webp)$/;
const PUBLIC_ASSETS = new Set(["/history.css", "/robots.txt"]);

const SECURITY_HEADERS = {
  "Cache-Control": "private, no-store",
  "Content-Security-Policy": "default-src 'none'; style-src 'self'; script-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
  "Referrer-Policy": "no-referrer",
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "X-Robots-Tag": "noindex, nofollow, noarchive",
};

function cookies(request) {
  const values = {};
  for (const part of (request.headers.get("Cookie") || "").split(";")) {
    const separator = part.indexOf("=");
    if (separator < 1) continue;
    values[part.slice(0, separator).trim()] = part.slice(separator + 1).trim();
  }
  return values;
}

function cookie(name, value, maxAge) {
  return `${name}=${value}; Path=/; Max-Age=${maxAge}; HttpOnly; Secure; SameSite=Lax`;
}

function secureResponse(response, setCookies = []) {
  const headers = new Headers(response.headers);
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
  for (const value of setCookies) headers.append("Set-Cookie", value);
  headers.set("Vary", "Cookie");
  return new Response(response.body, { status: response.status, statusText: response.statusText, headers });
}

function assetRequest(request, pathname, method = null) {
  const url = new URL(request.url);
  url.pathname = pathname;
  url.search = "";
  return new Request(url, {
    method: method || (request.method === "HEAD" ? "HEAD" : "GET"),
  });
}

async function fetchAsset(env, request, pathname, method = null) {
  return env.ASSETS.fetch(assetRequest(request, pathname, method));
}

async function blank(env, request, status, clear = false) {
  const response = await fetchAsset(env, request, "/404.html");
  const empty = new Response(response.body, { status, headers: response.headers });
  const expired = clear
    ? [cookie(SHARE_COOKIE, "", 0), cookie(VISIT_COOKIE, "", 0)]
    : [];
  return secureResponse(empty, expired);
}

async function metadata(env, request, token) {
  const response = await fetchAsset(env, request, `/__shares/${token}/meta.json`, "GET");
  if (!response.ok) return null;
  try {
    const value = await response.json();
    if (typeof value.id !== "string" || typeof value.label !== "string") return null;
    return value;
  } catch {
    return null;
  }
}

async function recordView(env, meta) {
  if (!env.VIEWS) return;
  const now = new Date().toISOString();
  await env.VIEWS.prepare(
    `INSERT INTO share_views (link_id, label, visit_count, first_seen, last_seen)
     VALUES (?, ?, 1, ?, ?)
     ON CONFLICT(link_id) DO UPDATE SET
       label = excluded.label,
       visit_count = share_views.visit_count + 1,
       last_seen = excluded.last_seen`,
  ).bind(meta.id, meta.label.slice(0, 120), now, now).run();
}

async function activate(request, env, token) {
  const meta = await metadata(env, request, token);
  if (!meta) return blank(env, request, 404);
  const expiresAt = meta.expires_at == null ? null : Number(meta.expires_at);
  const remaining = expiresAt == null
    ? YEAR_SECONDS
    : Math.floor(expiresAt - Date.now() / 1000);
  const maxAge = Number.isFinite(remaining)
    ? Math.max(1, Math.min(remaining, YEAR_SECONDS))
    : YEAR_SECONDS;
  const headers = new Headers({ Location: "/" });
  for (const [name, value] of Object.entries(SECURITY_HEADERS)) headers.set(name, value);
  headers.append("Set-Cookie", cookie(SHARE_COOKIE, token, maxAge));
  if (cookies(request)[SHARE_COOKIE] !== token) {
    headers.append("Set-Cookie", cookie(VISIT_COOKIE, "", 0));
  }
  return new Response(null, { status: 302, headers });
}

async function serveHistory(request, env, context, pathname) {
  const token = cookies(request)[SHARE_COOKIE];
  if (!token || !/^[A-Za-z0-9_-]{32,64}$/.test(token)) {
    return blank(env, request, pathname === "/" ? 200 : 404, Boolean(token));
  }

  const meta = await metadata(env, request, token);
  if (!meta) return blank(env, request, pathname === "/" ? 200 : 404, true);

  const isPoster = POSTER_PATH.test(pathname);
  let privatePath = isPoster ? pathname : `/__shares/${token}/`;
  if (pathname === "/watchlist" || pathname === "/watchlist/") {
    const type = new URL(request.url).searchParams.get("type");
    const suffix = type === "movie" || type === "tv" ? `${type}/` : "";
    privatePath = `/__shares/${token}/watchlist/${suffix}`;
  }

  const asset = await fetchAsset(env, request, privatePath);
  if (!asset.ok) return blank(env, request, 404);

  const currentCookies = cookies(request);
  const responseCookies = [];
  if (!isPoster && request.method === "GET" && currentCookies[VISIT_COOKIE] !== meta.id) {
    const write = recordView(env, meta).catch((error) => {
      console.error("View counter write failed", error);
    });
    if (context && typeof context.waitUntil === "function") context.waitUntil(write);
    else await write;
    responseCookies.push(cookie(VISIT_COOKIE, meta.id, VISIT_WINDOW_SECONDS));
  }
  return secureResponse(asset, responseCookies);
}

export async function handleRequest(request, env, context = {}) {
  if (request.method !== "GET" && request.method !== "HEAD") {
    return blank(env, request, 404);
  }

  const pathname = new URL(request.url).pathname;
  if (PUBLIC_ASSETS.has(pathname)) return secureResponse(await env.ASSETS.fetch(request));
  if (pathname.startsWith("/__shares/")) return blank(env, request, 404);

  const tokenMatch = pathname.match(TOKEN_PATH);
  if (tokenMatch) return activate(request, env, tokenMatch[1]);
  if (pathname === "/" || pathname === "/watchlist" || pathname === "/watchlist/" || POSTER_PATH.test(pathname)) {
    return serveHistory(request, env, context, pathname);
  }
  return blank(env, request, 404);
}

export default {
  fetch(request, env, context) {
    return handleRequest(request, env, context);
  },
};
