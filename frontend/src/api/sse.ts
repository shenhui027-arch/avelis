// SSE（text/event-stream）消费器：复用 apiFetch 的 Cookie / Bearer / Accept-Language / 401 单飞续期，
// 支持 JSON 回退、分片 UTF-8 流式解码、CRLF 归一、多行 data: 拼接、心跳注释过滤与 AbortSignal 清理。
import { t } from "../i18n";
import { apiFetch, parseApiError } from "./client";

export type SseEvent<T = unknown> = {
  event: string;
  data: T;
  rawData: string;
  id?: string;
};

export type SseResult<TJson = unknown> =
  | { mode: "sse" }
  | { mode: "json"; data: TJson };

export type ConsumeSseOptions = Omit<RequestInit, "signal"> & {
  onOpen?: (response: Response) => void;
  onEvent: (ev: SseEvent) => void;
  signal?: AbortSignal;
};

function parseFrame(frame: string): SseEvent | null {
  let eventName = "";
  let id: string | undefined;
  const dataLines: string[] = [];
  for (const line of frame.split("\n")) {
    if (!line || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "event") eventName = value;
    else if (field === "id") id = value;
    else if (field === "data") dataLines.push(value);
  }
  if (!dataLines.length) return null;
  const rawData = dataLines.join("\n");
  let data: unknown = rawData;
  try {
    data = JSON.parse(rawData);
  } catch {
    // 非 JSON 载荷按纯文本保留
  }
  return {
    event: eventName || "message",
    data,
    rawData,
    ...(id !== undefined ? { id } : {}),
  };
}

/** 创建增量 SSE 解析器，可用于流式块解析或单测。 */
export function createSseParser(onEvent: (ev: SseEvent) => void) {
  let buffer = "";
  const emitFrame = (raw: string) => {
    const parsed = parseFrame(raw);
    if (parsed) onEvent(parsed);
  };
  return {
    feed(chunk: string) {
      if (!chunk) return;
      buffer += chunk.replace(/\r\n?/g, "\n");
      let boundary = buffer.indexOf("\n\n");
      while (boundary !== -1) {
        const frame = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        emitFrame(frame);
        boundary = buffer.indexOf("\n\n");
      }
    },
    flush() {
      if (buffer.trim()) emitFrame(buffer);
      buffer = "";
    },
  };
}

async function readErrorBody(response: Response): Promise<unknown> {
  const ct = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (ct.includes("application/json")) {
    return response.json().catch(() => undefined);
  }
  const text = await response.text().catch(() => "");
  if (!text) return undefined;
  try {
    return JSON.parse(text);
  } catch {
    return { detail: text };
  }
}

export async function consumeSse<TJson = unknown>(
  url: string,
  options: ConsumeSseOptions,
): Promise<SseResult<TJson>> {
  const { onOpen, onEvent, signal, headers: initHeaders, ...rest } = options;
  if (signal?.aborted) return { mode: "sse" };

  const headers = new Headers(initHeaders);
  if (!headers.has("Accept")) headers.set("Accept", "text/event-stream, application/json");

  let response: Response;
  try {
    response = await apiFetch(url, {
      ...rest,
      headers,
      signal,
      throwOnError: false,
    });
  } catch (err) {
    if (signal?.aborted) return { mode: "sse" };
    throw err;
  }

  if (!response.ok) {
    const body = await readErrorBody(response);
    throw parseApiError(response.status, body, t("请求失败（{{status}}）", { status: response.status }));
  }

  const ct = response.headers.get("content-type")?.toLowerCase() ?? "";
  if (ct.includes("application/json") && !ct.includes("text/event-stream")) {
    const data = (await response.json()) as TJson;
    return { mode: "json", data };
  }

  onOpen?.(response);

  if (!response.body) {
    const text = await response.text().catch(() => "");
    const parser = createSseParser(onEvent);
    parser.feed(text);
    parser.flush();
    return { mode: "sse" };
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = createSseParser(onEvent);
  const onAbort = () => {
    void reader.cancel().catch(() => {});
  };
  signal?.addEventListener("abort", onAbort, { once: true });

  try {
    while (true) {
      if (signal?.aborted) {
        await reader.cancel().catch(() => {});
        return { mode: "sse" };
      }
      const { value, done } = await reader.read();
      if (done) break;
      if (value) parser.feed(decoder.decode(value, { stream: true }));
    }
    const tail = decoder.decode();
    if (tail) parser.feed(tail);
    parser.flush();
    return { mode: "sse" };
  } catch (err) {
    if (signal?.aborted) return { mode: "sse" };
    throw err;
  } finally {
    signal?.removeEventListener("abort", onAbort);
    reader.releaseLock();
  }
}
