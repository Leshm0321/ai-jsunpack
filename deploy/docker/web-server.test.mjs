import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { createWebServer, runtimeConfigFromEnvironment } from "./web-server.mjs";

test("runtime config reads a token file without exposing the signing secret", async () => {
  const root = await mkdtemp(join(tmpdir(), "ai-jsunpack-web-server-"));
  const tokenFile = join(root, "token");
  await writeFile(tokenFile, "file-token\n", "utf8");
  try {
    assert.deepEqual(
      runtimeConfigFromEnvironment({
        AI_JSUNPACK_AUTH_SECRET: "must-not-leak",
        AI_JSUNPACK_WEB_API_BASE_URL: "/api",
        AI_JSUNPACK_WEB_AUTH_TOKEN_FILE: tokenFile,
        AI_JSUNPACK_WEB_PROJECT_ID: "project-a",
        AI_JSUNPACK_WEB_USER_ID: "user-a",
      }),
      {
        apiBaseUrl: "/api",
        authToken: "file-token",
        projectId: "project-a",
        userId: "user-a",
      },
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("web server proxies /api and serves runtime config", async () => {
  const root = await mkdtemp(join(tmpdir(), "ai-jsunpack-web-server-"));
  await writeFile(join(root, "index.html"), "<h1>fixture</h1>", "utf8");
  const upstream = createServer((request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ authorization: request.headers.authorization, path: request.url }));
  });
  await new Promise((resolve) => upstream.listen(0, "127.0.0.1", resolve));
  const upstreamAddress = upstream.address();
  const web = createWebServer({
    root,
    environment: {
      AI_JSUNPACK_WEB_API_UPSTREAM: `http://127.0.0.1:${upstreamAddress.port}`,
      AI_JSUNPACK_WEB_AUTH_TOKEN: "runtime-token",
    },
  });
  await new Promise((resolve) => web.listen(0, "127.0.0.1", resolve));
  const webAddress = web.address();
  try {
    const config = await fetch(`http://127.0.0.1:${webAddress.port}/runtime-config.js`).then((response) => response.text());
    assert.match(config, /runtime-token/);
    const proxied = await fetch(`http://127.0.0.1:${webAddress.port}/api/health?full=1`, {
      headers: { Authorization: "Bearer browser-token" },
    }).then((response) => response.json());
    assert.deepEqual(proxied, { authorization: "Bearer browser-token", path: "/health?full=1" });
  } finally {
    await Promise.all([
      new Promise((resolve) => web.close(resolve)),
      new Promise((resolve) => upstream.close(resolve)),
    ]);
    await rm(root, { recursive: true, force: true });
  }
});
