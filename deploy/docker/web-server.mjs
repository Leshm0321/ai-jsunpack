import { createReadStream, existsSync, readFileSync, statSync } from "node:fs";
import { createServer, request as httpRequest } from "node:http";
import { request as httpsRequest } from "node:https";
import { extname, isAbsolute, join, normalize, relative, resolve } from "node:path";
import { pathToFileURL } from "node:url";

const hopByHopHeaders = new Set([
  "connection",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailers",
  "transfer-encoding",
  "upgrade",
]);

const contentTypes = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".ttf": "font/ttf",
  ".wasm": "application/wasm",
};

export function createWebServer({ root = resolve("dist"), environment = process.env } = {}) {
  const apiUpstream = validatedUpstream(environment.AI_JSUNPACK_WEB_API_UPSTREAM || "http://api:8000");
  return createServer((request, response) => {
    const requestUrl = new URL(request.url || "/", "http://web.local");
    if (requestUrl.pathname === "/runtime-config.js") {
      try {
        const body = runtimeConfigJavaScript(environment);
        response.writeHead(200, {
          "Cache-Control": "no-store",
          "Content-Length": Buffer.byteLength(body),
          "Content-Type": "application/javascript; charset=utf-8",
        });
        response.end(body);
      } catch (error) {
        response.writeHead(500, { "Content-Type": "text/plain; charset=utf-8" });
        response.end(`运行时配置不可用：${error.message}`);
      }
      return;
    }
    if (requestUrl.pathname === "/api" || requestUrl.pathname.startsWith("/api/")) {
      proxyApiRequest(request, response, requestUrl, apiUpstream);
      return;
    }
    serveStatic(response, requestUrl.pathname, root);
  });
}

export function runtimeConfigFromEnvironment(environment = process.env) {
  return {
    apiBaseUrl: cleanValue(environment.AI_JSUNPACK_WEB_API_BASE_URL) || "/api",
    authToken: secretValue(
      environment.AI_JSUNPACK_WEB_AUTH_TOKEN,
      environment.AI_JSUNPACK_WEB_AUTH_TOKEN_FILE,
    ),
    userId: cleanValue(environment.AI_JSUNPACK_WEB_USER_ID) || "local-user",
    projectId: cleanValue(environment.AI_JSUNPACK_WEB_PROJECT_ID) || "default",
  };
}

export function runtimeConfigJavaScript(environment = process.env) {
  return `window.__AI_JSUNPACK_CONFIG__ = ${JSON.stringify(runtimeConfigFromEnvironment(environment))};\n`;
}

function secretValue(rawValue, rawFile) {
  const direct = cleanValue(rawValue);
  if (direct) {
    return direct;
  }
  const file = cleanValue(rawFile);
  if (!file) {
    return "";
  }
  return readFileSync(file, "utf8").trim();
}

function cleanValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function validatedUpstream(value) {
  const upstream = new URL(value);
  if (!new Set(["http:", "https:"]).has(upstream.protocol) || !upstream.hostname) {
    throw new Error("AI_JSUNPACK_WEB_API_UPSTREAM 必须是包含主机名的 HTTP(S) URL");
  }
  if (upstream.username || upstream.password || upstream.pathname !== "/" || upstream.search || upstream.hash) {
    throw new Error("AI_JSUNPACK_WEB_API_UPSTREAM 不得包含凭据、路径、查询参数或片段");
  }
  return upstream;
}

function proxyApiRequest(clientRequest, clientResponse, requestUrl, upstream) {
  const upstreamPath = `${requestUrl.pathname.slice(4) || "/"}${requestUrl.search}`;
  const headers = filteredHeaders(clientRequest.headers);
  headers.host = upstream.host;
  const transport = upstream.protocol === "https:" ? httpsRequest : httpRequest;
  const proxyRequest = transport(
    {
      protocol: upstream.protocol,
      hostname: upstream.hostname,
      port: upstream.port,
      method: clientRequest.method,
      path: upstreamPath,
      headers,
    },
    (proxyResponse) => {
      clientResponse.writeHead(proxyResponse.statusCode || 502, filteredHeaders(proxyResponse.headers));
      proxyResponse.pipe(clientResponse);
    },
  );
  proxyRequest.on("error", (error) => {
    if (!clientResponse.headersSent) {
      clientResponse.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    }
    clientResponse.end(`API 上游不可用：${error.message}`);
  });
  clientRequest.pipe(proxyRequest);
}

function filteredHeaders(headers) {
  return Object.fromEntries(
    Object.entries(headers).filter(([name, value]) => value !== undefined && !hopByHopHeaders.has(name.toLowerCase())),
  );
}

function serveStatic(response, pathname, root) {
  const filePath = safePath(pathname, root);
  if (filePath === null || !existsSync(filePath)) {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
    response.end("未找到");
    return;
  }
  response.writeHead(200, {
    "Cache-Control": filePath.endsWith("index.html") ? "no-cache" : "public, max-age=31536000, immutable",
    "Content-Type": contentTypes[extname(filePath)] || "application/octet-stream",
  });
  createReadStream(filePath).pipe(response);
}

function safePath(urlPath, root) {
  const candidate = resolve(root, normalize(urlPath).replace(/^([/\\])+/, ""));
  const relativePath = relative(root, candidate);
  if (relativePath.startsWith("..") || isAbsolute(relativePath)) {
    return null;
  }
  if (existsSync(candidate) && statSync(candidate).isFile()) {
    return candidate;
  }
  return join(root, "index.html");
}

const entrypoint = process.argv[1] ? pathToFileURL(resolve(process.argv[1])).href : "";
if (import.meta.url === entrypoint) {
  const port = Number(process.env.PORT || 5173);
  createWebServer().listen(port, "0.0.0.0");
}
