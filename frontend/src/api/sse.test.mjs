// Run: node src/api/sse.test.mjs (from frontend/).
// SSE 解析与流消费单测：分块 UTF-8、多帧合并、CRLF、注释心跳、多行 data、JSON 回退、429 错误与 AbortSignal。
import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

const root = fileURLToPath(new URL("../..", import.meta.url));

function setupGlobals() {
  const store = new Map([["avelis.lang", "zh-CN"]]);
  const storage = { getItem: (k) => store.get(k) ?? null, setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
  Object.assign(globalThis, {
    localStorage: storage,
    sessionStorage: storage,
    location: { pathname: "/", search: "", origin: "http://localhost", assign() {} },
    document: { documentElement: { lang: "zh-CN", dataset: {} }, cookie: "", addEventListener() {}, removeEventListener() {} },
    window: { localStorage: storage, addEventListener() {}, removeEventListener() {} },
  });
}

setupGlobals();
const server = await createServer({ root, logLevel: "error", appType: "custom", server: { middlewareMode: true, hmr: false } });

try {
  const { consumeSse, createSseParser } = await server.ssrLoadModule("/src/api/sse.ts");
  const { ApiError } = await server.ssrLoadModule("/src/api/client.ts");

  // 1. 纯解析器：CRLF、注释心跳、多行 data 拼接、JSON 与纯文本
  {
    const events = [];
    const p = createSseParser((ev) => events.push(ev));
    p.feed(": keepalive\r\n\r\n");
    p.feed("id: 1\r\nevent: delta\r\ndata: {\"delta\":\"你\"}\r\n\r\n");
    p.feed("event: note\rdata: line1\rdata: line2\r\n\r\n");
    p.flush();
    assert.equal(events.length, 2);
    assert.deepEqual(events[0], { event: "delta", id: "1", rawData: "{\"delta\":\"你\"}", data: { delta: "你" } });
    assert.deepEqual(events[1], { event: "note", rawData: "line1\nline2", data: "line1\nline2" });
  }

  // 2. 分块 UTF-8 跨 chunk 边界 + 多事件同 chunk
  {
    const encoder = new TextEncoder();
    const full = encoder.encode("event: thinking\ndata: {\"delta\":\"深度推理中\"}\n\nevent: done\ndata: {\"ok\":true}\n\n");
    // 故意在多字节中文字符中间切开
    const split1 = 27;
    const split2 = 34;
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(full.slice(0, split1));
        controller.enqueue(full.slice(split1, split2));
        controller.enqueue(full.slice(split2));
        controller.close();
      },
    });
    const origFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream; charset=utf-8" } });
    try {
      const seen = [];
      let opened = false;
      const res = await consumeSse("/api/ask/stream", {
        method: "POST",
        onOpen: () => { opened = true; },
        onEvent: (ev) => seen.push(ev),
      });
      assert.equal(opened, true);
      assert.deepEqual(res, { mode: "sse" });
      assert.equal(seen.length, 2);
      assert.equal(seen[0].event, "thinking");
      assert.deepEqual(seen[0].data, { delta: "深度推理中" });
      assert.equal(seen[1].event, "done");
      assert.deepEqual(seen[1].data, { ok: true });
    } finally {
      globalThis.fetch = origFetch;
    }
  }

  // 3. application/json 回退模式
  {
    const origFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(JSON.stringify({ id: "ans-1", status: "answered" }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
    try {
      const events = [];
      const res = await consumeSse("/api/ask/stream", { method: "POST", onEvent: (ev) => events.push(ev) });
      assert.equal(res.mode, "json");
      assert.deepEqual(res.data, { id: "ans-1", status: "answered" });
      assert.equal(events.length, 0);
    } finally {
      globalThis.fetch = origFetch;
    }
  }

  // 4. HTTP 429 配额/限流错误解析
  {
    const origFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: { code: "quota_exceeded", detail: "已达到模型用量或费用配额上限" } }), {
      status: 429,
      headers: { "Content-Type": "application/json" },
    });
    try {
      await assert.rejects(
        () => consumeSse("/api/ask/stream", { method: "POST", onEvent() {} }),
        (err) => err instanceof ApiError && err.status === 429 && err.code === "quota_exceeded" && err.message.includes("配额上限"),
      );
    } finally {
      globalThis.fetch = origFetch;
    }
  }

  // 5. AbortSignal 干净中止流，不抛异常
  {
    const ctrl = new AbortController();
    let cancelled = false;
    const stream = new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("event: status\ndata: {\"status\":\"running\"}\n\n"));
      },
      cancel() {
        cancelled = true;
      },
    });
    const origFetch = globalThis.fetch;
    globalThis.fetch = async () => new Response(stream, { status: 200, headers: { "Content-Type": "text/event-stream" } });
    try {
      const seen = [];
      const res = await consumeSse("/api/analysis/runs/r1/stream", {
        signal: ctrl.signal,
        onEvent: (ev) => {
          seen.push(ev);
          ctrl.abort();
        },
      });
      assert.deepEqual(res, { mode: "sse" });
      assert.equal(seen.length, 1);
      assert.equal(cancelled, true);
    } finally {
      globalThis.fetch = origFetch;
    }
  }

  console.log("sse: ok");
} finally {
  await server.close();
}
