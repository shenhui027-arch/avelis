import { useRef, useState, type DragEvent, type KeyboardEvent } from "react";

import { ApiError, token, type Schemas } from "../../api/client";
import { errorText } from "../../api/hooks";
import { Banner, Drawer, Icon, Tile, useMessage } from "../../components/ui";
import { currentLanguage, t } from "../../i18n";

export type UploadPreset = { id: string; name: string; version: number | null };

type ValidationOut = Schemas["SkillValidationOut"];
type ConflictChoice = "rename" | "overwrite";

const MB = 1024 * 1024;
export const formatSkillBytes = (bytes: number) =>
  bytes >= MB ? `${(bytes / MB).toFixed(1)} MB` : bytes >= 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${bytes} B`;

/** 技能包 multipart 上传、校验与 zip 下载共用的请求封装：对齐 api/client.ts 的 401 会话过期处理与错误提取。 */
export async function skillFetch(url: string, init?: RequestInit): Promise<Response> {
  const headers = new Headers(init?.headers);
  const tk = token.get();
  if (tk) headers.set("Authorization", `Bearer ${tk}`);
  headers.set("Accept-Language", currentLanguage());
  let resp = await fetch(url, { ...init, headers });
  if (resp.status === 401) {
    const latest = token.get();
    if (tk && latest && latest !== tk) {
      headers.set("Authorization", `Bearer ${latest}`);
      resp = await fetch(url, { ...init, headers });
    }
  }
  if (resp.ok) return resp;
  if (resp.status === 401) {
    token.clear();
    if (!location.pathname.startsWith("/login")) {
      const from = encodeURIComponent(location.pathname + location.search);
      location.assign(`/login?reason=expired&from=${from}`);
    }
  }
  const body = await resp.json().catch(() => null);
  const detail = (body as { detail?: unknown } | null)?.detail;
  const detailObjMsg = detail && typeof detail === "object" && !Array.isArray(detail)
    ? (detail as { message?: unknown }).message
    : undefined;
  const message = typeof detail === "string" && detail.trim()
    ? detail
    : typeof detailObjMsg === "string" && detailObjMsg.trim()
      ? detailObjMsg
      : Array.isArray(detail)
        ? t("输入不合法")
        : resp.status === 413
          ? t("文件超出大小限制")
          : resp.status === 401
            ? t("会话已过期或已在其他位置失效，请重新登录")
            : resp.status === 403
              ? t("没有访问权限")
              : t("请求失败（{{status}}）", { status: resp.status });
  throw new ApiError(resp.status, message, body);
}

export function UploadDrawer({ preset, onClose, onSubmitted }: {
  preset?: UploadPreset | null;
  onClose: () => void;
  onSubmitted: () => void;
}) {
  const inputRef = useRef<HTMLInputElement>(null);
  const reqIdRef = useRef(0);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [validating, setValidating] = useState(false);
  const [validation, setValidation] = useState<ValidationOut | null>(null);
  const [choice, setChoice] = useState<ConflictChoice>("rename");
  const [note, setNote] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [message, setMessage] = useMessage();

  async function selectFile(picked: File) {
    const reqId = ++reqIdRef.current;
    setFile(picked);
    setValidation(null);
    setMessage(null);
    setValidating(true);
    try {
      const form = new FormData();
      form.append("file", picked);
      const resp = await skillFetch("/api/skills/validate", { method: "POST", body: form });
      const out = (await resp.json()) as ValidationOut;
      if (reqId !== reqIdRef.current) return;
      setValidation(out);
      if (out.conflict) {
        const allowOverwrite = out.conflict.can_overwrite && !out.conflict.has_pending;
        const matchesPreset = !!preset && (preset.id === out.conflict.skill_id || preset.name === out.conflict.name);
        setChoice(allowOverwrite && matchesPreset ? "overwrite" : "rename");
      } else {
        setChoice("rename");
      }
    } catch (e) {
      if (reqId === reqIdRef.current) setMessage({ ok: false, text: errorText(e) });
    } finally {
      if (reqId === reqIdRef.current) setValidating(false);
    }
  }

  function clearFile() {
    reqIdRef.current += 1;
    setFile(null);
    setValidation(null);
    setValidating(false);
    setMessage(null);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>) {
    e.preventDefault();
    setDragging(false);
    const picked = e.dataTransfer.files?.[0];
    if (picked) void selectFile(picked);
  }

  const conflict = validation?.conflict ?? null;
  const canOverwrite = !!conflict && conflict.can_overwrite && !conflict.has_pending;
  const hasError = !!validation && (!validation.ok || validation.checks.some((c) => c.level === "error"));
  const presetMismatch = !!preset && !!validation && (
    (conflict != null && conflict.skill_id !== preset.id && conflict.name !== preset.name)
    || (validation.name != null && validation.name !== preset.name)
  );
  const canSubmit = !!file && !validating && !submitting && !!validation && !hasError
    && (!conflict || (canOverwrite && choice === "overwrite"))
    && note.length <= 500;

  function onRadioKey(e: KeyboardEvent<HTMLDivElement>, current: ConflictChoice) {
    if (e.key === " " || e.key === "Enter") {
      e.preventDefault();
      if (current === "rename") setChoice("rename");
      else if (canOverwrite) setChoice("overwrite");
      return;
    }
    if ((e.key === "ArrowDown" || e.key === "ArrowUp" || e.key === "ArrowLeft" || e.key === "ArrowRight") && canOverwrite) {
      e.preventDefault();
      const next: ConflictChoice = current === "rename" ? "overwrite" : "rename";
      setChoice(next);
      const group = e.currentTarget.parentElement;
      group?.querySelector<HTMLElement>(`[data-choice="${next}"]`)?.focus();
    }
  }

  async function handleSubmit() {
    if (!canSubmit || !file || !validation) return;
    setSubmitting(true);
    setMessage(null);
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("mode", conflict ? "overwrite" : "new");
      if (note.trim()) form.append("note", note.trim());
      await skillFetch("/api/skills/upload", { method: "POST", body: form });
      onSubmitted();
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <Drawer
      title={t("上传技能")}
      width={520}
      onClose={() => { if (!submitting) onClose(); }}
      footer={
        <>
          <span className="av-skill-foot-note"><Icon name="audit" small />{t("提交后由管理员审核，上架后出现在技能市场")}</span>
          <span className="av-skills__spacer" />
          <button type="button" className="av-btn av-btn--ghost" disabled={submitting} onClick={onClose}>{t("取消")}</button>
          <button type="button" className="av-btn av-btn--primary" disabled={!canSubmit} onClick={handleSubmit}>
            {submitting ? t("提交中…") : t("提交审核")}
          </button>
        </>
      }
    >
      <input
        ref={inputRef}
        type="file"
        accept=".zip"
        hidden
        onChange={(e) => {
          const picked = e.target.files?.[0];
          if (picked) void selectFile(picked);
          e.target.value = "";
        }}
      />
      <div
        role="button"
        tabIndex={0}
        className={`av-skill-drop${dragging ? " is-dragging" : ""}`}
        onClick={() => { if (!submitting) inputRef.current?.click(); }}
        onKeyDown={(e) => { if (!submitting && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); inputRef.current?.click(); } }}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        <Icon name="upload" />
        <strong>{t("拖入技能 zip 包，或点击选择")}</strong>
        <span>{t("遵循 Agent Skills 规范：包内一个技能目录，含 SKILL.md")}</span>
      </div>

      {file ? (
        <div className="av-skill-file">
          <Tile tone="ask" icon="skill" size="sm" />
          <span className="av-skill-file__body">
            <span className="av-skill-file__name">{file.name}</span>
            <small>
              {validating
                ? `${formatSkillBytes(file.size)} · ${t("校验中…")}`
                : validation
                  ? `${formatSkillBytes(validation.zip_bytes || file.size)} · ${t("{{count}} 个文件", { count: validation.files.length })}`
                  : formatSkillBytes(file.size)}
            </small>
          </span>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon"
            aria-label={t("移除文件")} disabled={submitting} onClick={clearFile}>
            <Icon name="close" />
          </button>
        </div>
      ) : null}

      {validation && validation.checks.length > 0 ? (
        <div className="av-skill-sec">
          <h3>{t("校验")}</h3>
          <div className="av-skill-checks">
            {validation.checks.map((c, i) => (
              <div key={i} className={`av-skill-check is-${c.level}`}>
                <Icon name={c.level === "ok" ? "check" : c.level === "error" ? "close" : "info"} small />
                <span>{c.message}</span>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      {presetMismatch && preset && validation ? (
        <div className="av-banner av-banner--warn">
          {t("所选技能包名称（{{actual}}）与当前技能（{{expected}}）不一致", {
            actual: validation.name ?? conflict?.name ?? "",
            expected: preset.name,
          })}
        </div>
      ) : null}

      {conflict ? (
        <>
          <div className="av-banner av-banner--warn">
            {conflict.version != null
              ? t("已存在同名技能 {{name}}（v{{version}}，{{owner}} 上传，{{count}} 人启用）", {
                  name: conflict.name, version: conflict.version, owner: conflict.owner.display_name, count: conflict.enabled_users,
                })
              : t("已存在同名技能 {{name}}（{{owner}} 上传，{{count}} 人启用）", {
                  name: conflict.name, owner: conflict.owner.display_name, count: conflict.enabled_users,
                })}
          </div>
          {conflict.has_pending ? (
            <div className="av-banner av-banner--danger">{t("该技能已有待审核的版本")}</div>
          ) : null}
          <div className="av-skill-choices" role="radiogroup" aria-label={t("同名冲突处理")}>
            <div
              role="radio"
              data-choice="rename"
              aria-checked={choice === "rename"}
              tabIndex={choice === "rename" ? 0 : -1}
              className="av-skill-opt"
              onClick={() => setChoice("rename")}
              onKeyDown={(e) => onRadioKey(e, "rename")}
            >
              <i aria-hidden />
              <span>
                <strong>{t("修改后重新上传")}</strong>
                <small>{t("在 SKILL.md 里改 name 与目录名，再上传为新技能")}</small>
              </span>
            </div>
            <div
              role="radio"
              data-choice="overwrite"
              aria-checked={choice === "overwrite"}
              aria-disabled={!canOverwrite || undefined}
              tabIndex={canOverwrite && choice === "overwrite" ? 0 : -1}
              className="av-skill-opt"
              onClick={() => { if (canOverwrite) setChoice("overwrite"); }}
              onKeyDown={(e) => onRadioKey(e, "overwrite")}
            >
              <i aria-hidden />
              <span>
                <strong>{t("覆盖为 v{{version}}", { version: validation!.next_version })}</strong>
                <small>
                  {!conflict.can_overwrite
                    ? t("只有上传人或管理员可以覆盖")
                    : conflict.has_pending
                      ? t("该技能已有待审核的版本")
                      : t("审核通过后替换；审核期间已启用的人继续使用当前版本")}
                </small>
              </span>
            </div>
          </div>
        </>
      ) : null}

      <label className="av-field">
        <span className="av-label">{t("版本说明")}</span>
        <textarea
          className="av-textarea"
          rows={2}
          maxLength={500}
          value={note}
          placeholder={t("选填，最多 500 字")}
          onChange={(e) => setNote(e.target.value)}
        />
      </label>

      <Banner message={message} />
    </Drawer>
  );
}
