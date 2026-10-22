import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/sharing/static_site/worker.mjs";

const TOKEN = "abcdefghijklmnopqrstuvwxyzABCDEF";
const ID = "4d8f2a6bc76140d1";
const POSTER = "/posters/0123456789abcdefabcd.jpg";

function setup({ active = true } = {}) {
  const requested = [];
  const assetCalls = [];
  const writes = [];
  const pending = [];
  const env = {
    ASSETS: {
      async fetch(request) {
        const path = new URL(request.url).pathname;
        requested.push(path);
        assetCalls.push({ path, method: request.method });
        if (path === "/404.html") return new Response("<!doctype html><body></body>");
        if (path === `/__shares/${TOKEN}/meta.json` && active) {
          return Response.json({ id: ID, label: "Brother", expires_at: null });
        }
        if (path.endsWith("/index.html")) {
          return new Response(null, { status: 308, headers: { Location: path.slice(0, -10) } });
        }
        if (path === `/__shares/${TOKEN}/`) {
          return new Response("<main>current week</main>", { headers: { "Content-Type": "text/html" } });
        }
        if (path === `/__shares/${TOKEN}/2026/week-37/`) {
          return new Response("<main>week 37</main>", { headers: { "Content-Type": "text/html" } });
        }
        if (path.startsWith(`/__shares/${TOKEN}/watchlist/`)) {
          return new Response("<main>watchlist</main>", { headers: { "Content-Type": "text/html" } });
        }
        if (path === POSTER) {
          return new Response(request.method === "HEAD" ? null : "poster-bytes", {
            headers: { "Content-Type": "image/jpeg" },
          });
        }
        if (path === "/history.css") return new Response("body{}");
        return new Response("missing", { status: 404 });
      },
    },
    VIEWS: {
      prepare(sql) {
        return {
          bind(...values) {
            return {
              async run() {
                writes.push({ sql, values });
              },
            };
          },
        };
      },
    },
  };
  const context = { waitUntil(promise) { pending.push(promise); } };
  return { env, context, pending, requested, assetCalls, writes };
}

function request(path, cookie = "", method = "GET") {
  const headers = cookie ? { Cookie: cookie } : {};
  return new Request(`https://intake.example${path}`, { method, headers });
}

test("a valid share URL stores the token and redirects to root", async () => {
  const state = setup();
  const response = await handleRequest(request(`/s/${TOKEN}/`), state.env, state.context);

  assert.equal(response.status, 302);
  assert.equal(response.headers.get("Location"), "/");
  assert.match(response.headers.get("Set-Cookie"), /__Host-intake_share=.*Max-Age=31536000.*HttpOnly.*Secure.*SameSite=Lax/);
  assert.equal(state.writes.length, 0);
});

test("reopening the same share URL preserves the visit window", async () => {
  const state = setup();
  const response = await handleRequest(
    request(
      `/s/${TOKEN}/`,
      `__Host-intake_share=${TOKEN}; __Host-intake_visit=${ID}`,
    ),
    state.env,
    state.context,
  );

  assert.equal(response.status, 302);
  assert.doesNotMatch(response.headers.get("Set-Cookie"), /__Host-intake_visit=;/);
});

test("root is blank without a valid share cookie", async () => {
  const state = setup();
  const response = await handleRequest(request("/"), state.env, state.context);

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "<!doctype html><body></body>");
  assert.equal(state.writes.length, 0);
});

test("root serves private history and records one visit", async () => {
  const state = setup();
  const response = await handleRequest(
    request("/", `__Host-intake_share=${TOKEN}`),
    state.env,
    state.context,
  );
  await Promise.all(state.pending);

  assert.equal(response.status, 200);
  assert.equal(await response.text(), "<main>current week</main>");
  assert.match(response.headers.get("Set-Cookie"), new RegExp(`__Host-intake_visit=${ID}`));
  assert.equal(state.writes.length, 1);
  assert.deepEqual(state.writes[0].values.slice(0, 2), [ID, "Brother"]);
  assert.doesNotMatch(state.writes[0].sql, new RegExp(TOKEN));
});

test("the 30-minute visit cookie prevents refresh inflation", async () => {
  const state = setup();
  const response = await handleRequest(
    request("/", `__Host-intake_share=${TOKEN}; __Host-intake_visit=${ID}`),
    state.env,
    state.context,
  );

  assert.equal(response.status, 200);
  assert.equal(state.pending.length, 0);
  assert.equal(state.writes.length, 0);
});

test("HEAD authorization fetches metadata with GET and does not mutate counters", async () => {
  const state = setup();
  const response = await handleRequest(
    request("/", `__Host-intake_share=${TOKEN}`, "HEAD"),
    state.env,
    state.context,
  );

  assert.equal(response.status, 200);
  assert.equal(state.pending.length, 0);
  assert.equal(state.writes.length, 0);
  assert.equal(response.headers.get("Set-Cookie"), null);
  const metadataCall = state.assetCalls.find(({ path }) => path.endsWith("/meta.json"));
  assert.equal(metadataCall.method, "GET");
});

test("old weekly routes are blank even with a valid cookie", async () => {
  const state = setup();
  const response = await handleRequest(
    request("/history/2026/week-37/", `__Host-intake_share=${TOKEN}`),
    state.env,
    state.context,
  );
  assert.equal(response.status, 404);
  assert.equal(await response.text(), "<!doctype html><body></body>");
  assert.equal(state.writes.length, 0);
});

test("private assets and unknown routes cannot be requested directly", async () => {
  const state = setup();
  const privateResponse = await handleRequest(
    request(`/__shares/${TOKEN}/index.html`),
    state.env,
    state.context,
  );
  const unknownResponse = await handleRequest(request("/anything"), state.env, state.context);

  assert.equal(privateResponse.status, 404);
  assert.equal(unknownResponse.status, 404);
});

test("authorized poster requests preserve image headers and never count visits", async () => {
  for (const method of ["GET", "HEAD"]) {
    const state = setup();
    const response = await handleRequest(
      request(POSTER, `__Host-intake_share=${TOKEN}`, method), state.env, state.context,
    );
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("Content-Type"), "image/jpeg");
    assert.equal(response.headers.get("Cache-Control"), "private, no-store");
    assert.equal(response.headers.get("Set-Cookie"), null);
    assert.equal(await response.text(), method === "HEAD" ? "" : "poster-bytes");
    assert.equal(state.pending.length, 0);
    assert.equal(state.writes.length, 0);
  }
});

test("posters reject anonymous, malformed, and revoked share cookies", async () => {
  for (const [active, cookie] of [
    [true, ""],
    [true, "__Host-intake_share=invalid"],
    [false, `__Host-intake_share=${TOKEN}`],
  ]) {
    const state = setup({ active });
    const response = await handleRequest(request(POSTER, cookie), state.env, state.context);
    assert.equal(response.status, 404);
    assert.ok(!state.requested.includes(POSTER));
    assert.equal(state.writes.length, 0);
  }
});

test("missing or unrecognized poster paths remain blank", async () => {
  for (const path of ["/posters/ffffffffffffffffffff.jpg", "/posters/test.svg"]) {
    const state = setup();
    const response = await handleRequest(
      request(path, `__Host-intake_share=${TOKEN}`), state.env, state.context,
    );
    assert.equal(response.status, 404);
    assert.equal(await response.text(), "<!doctype html><body></body>");
    assert.equal(state.writes.length, 0);
  }
});

test("watchlist filters resolve only to allowed private snapshots", async () => {
  for (const [path, suffix] of [
    ["/watchlist", ""], ["/watchlist/?type=all", ""],
    ["/watchlist/?type=movie", "movie/"], ["/watchlist/?type=tv", "tv/"],
    ["/watchlist/?type=../../meta.json", ""],
  ]) {
    const state = setup();
    const response = await handleRequest(
      request(path, `__Host-intake_share=${TOKEN}; __Host-intake_visit=${ID}`),
      state.env, state.context,
    );
    assert.equal(response.status, 200);
    assert.ok(state.requested.includes(`/__shares/${TOKEN}/watchlist/${suffix}`));
    assert.equal(state.writes.length, 0);
    assert.equal(response.headers.get("Cache-Control"), "private, no-store");
  }
});

test("watchlist rejects anonymous and revoked viewers", async () => {
  for (const [active, cookie] of [[true, ""], [false, `__Host-intake_share=${TOKEN}`]]) {
    const state = setup({ active });
    const response = await handleRequest(request("/watchlist/", cookie), state.env, state.context);
    assert.equal(response.status, 404);
    assert.equal(await response.text(), "<!doctype html><body></body>");
    assert.ok(!state.requested.some((path) => path.includes("/watchlist/")));
    assert.equal(state.writes.length, 0);
  }
});

test("revoked tokens become blank and clear stored cookies", async () => {
  const state = setup({ active: false });
  const response = await handleRequest(
    request("/", `__Host-intake_share=${TOKEN}; __Host-intake_visit=${ID}`),
    state.env,
    state.context,
  );

  assert.equal(response.status, 200);
  assert.match(response.headers.get("Set-Cookie"), /__Host-intake_share=;.*Max-Age=0/);
});
