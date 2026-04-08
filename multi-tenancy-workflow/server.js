// Tiny zero-dependency static file server.
// Serves the contents of this folder over HTTP.
// Usage: `npm start` (or `node server.js`).

import { createServer } from "node:http";
import { readFile, stat } from "node:fs/promises";
import { extname, join, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const ROOT = resolve(fileURLToPath(new URL(".", import.meta.url)));
const PORT = Number(process.env.PORT) || 8765;
const HOST = process.env.HOST || "127.0.0.1";

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js":   "text/javascript; charset=utf-8",
  ".mjs":  "text/javascript; charset=utf-8",
  ".css":  "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg":  "image/svg+xml",
  ".png":  "image/png",
  ".jpg":  "image/jpeg",
  ".ico":  "image/x-icon",
  ".map":  "application/json; charset=utf-8",
  ".txt":  "text/plain; charset=utf-8",
  ".md":   "text/markdown; charset=utf-8",
};

function safeJoin(root, urlPath) {
  // Decode + strip query/hash, prevent path traversal.
  const clean = decodeURIComponent(urlPath.split("?")[0].split("#")[0]);
  const joined = normalize(join(root, clean));
  if (!joined.startsWith(root)) return null;
  return joined;
}

async function resolveFile(filePath) {
  try {
    const s = await stat(filePath);
    if (s.isDirectory()) {
      const indexPath = join(filePath, "index.html");
      const s2 = await stat(indexPath);
      if (s2.isFile()) return indexPath;
      return null;
    }
    return s.isFile() ? filePath : null;
  } catch {
    return null;
  }
}

const server = createServer(async (req, res) => {
  const filePath = safeJoin(ROOT, req.url || "/");
  if (!filePath) {
    res.writeHead(400).end("Bad Request");
    return;
  }
  const resolved = await resolveFile(filePath);
  if (!resolved) {
    res.writeHead(404, { "Content-Type": "text/plain" }).end("Not Found");
    return;
  }
  try {
    const body = await readFile(resolved);
    const type = MIME[extname(resolved).toLowerCase()] || "application/octet-stream";
    res.writeHead(200, {
      "Content-Type": type,
      "Cache-Control": "no-cache",
    }).end(body);
  } catch (err) {
    res.writeHead(500, { "Content-Type": "text/plain" }).end(`Server error: ${err.message}`);
  }
});

server.listen(PORT, HOST, () => {
  console.log(`multi-tenancy-workflow → http://${HOST}:${PORT}/`);
  console.log("Press Ctrl+C to stop.");
});
