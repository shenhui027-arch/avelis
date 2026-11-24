// 问数回答飞轮组件：赞/踩反馈（含可选原因弹条）、VQR 与语义缓存角标、数值核验提示、存为标准查询。
import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { errorText, useAction, type Answer } from "../api/hooks";
import { useRoles } from "../layout/Shell";
import { VqrEditorDrawer } from "../pages/catalog/VqrDrawer";
import { t } from "../i18n";
import { Icon } from "./Icon";
import { useOverlay } from "./overlay";
import { formatSql, Pill } from "./ui";

export type VqrRef = { id: string; title: string; metric_codes?: string[] };
export type GroundingInfo = { ok?: boolean; checked_count?: number; ungrounded?: string[]; fallback?: string };
export type SemanticCacheInfo = { hit?: boolean; similarity?: number };

export function parseAnswerEvidence(a: Answer) {
  const ev = (a.evidence ?? {}) as Record<string, unknown>;
  const vqrRefs = Array.isArray(ev.vqr_refs) ? (ev.vqr_refs as VqrRef[]) : [];
  const semanticCache = ev.semantic_cache && typeof ev.semantic_cache === "object"
    ? (ev.semantic_cache as SemanticCacheInfo)
    : undefined;
  const grounding = ev.explanation_grounding && typeof ev.explanation_grounding === "object"
    ? (ev.explanation_grounding as GroundingInfo)
    : undefined;
  return { vqrRefs, semanticCache, grounding };
}

export function AnswerEvidenceBadges({ a }: { a: Answer }) {
  const { vqrRefs, semanticCache } = parseAnswerEvidence(a);
  return (
    <>
      {vqrRefs.length ? (
        <Link
          to={`/catalog/queries?id=${encodeURIComponent(vqrRefs[0].id)}`}
          className="av-pill av-pill--info av-vqr-badge"
          title={vqrRefs.map((v) => v.title).join(" · ")}
          onClick={(e) => e.stopPropagation()}
        >
          <Icon name="check" small />
          {t("标准查询")}
          {vqrRefs.length > 1 ? ` · ${vqrRefs.length}` : ""}
        </Link>
      ) : null}
      {a.cache_hit || semanticCache?.hit ? (
        semanticCache?.hit ? (
          <Pill
            tone="info"
            title={typeof semanticCache.similarity === "number"
              ? t("语义相似度 {{pct}}%", { pct: Math.round(semanticCache.similarity * 100) })
              : undefined}
          >
            {t("语义缓存")}
          </Pill>
        ) : (
          <span className="av-tag">{t("缓存")}</span>
        )
      ) : null}
    </>
  );
}

export function GroundingWarning({ grounding }: { grounding?: GroundingInfo | null }) {
  if (!grounding || grounding.ok !== false) return null;
  const nums = Array.isArray(grounding.ungrounded) ? grounding.ungrounded.filter(Boolean) : [];
  return (
    <span className="av-grounding-warn">
      <Pill
        tone="warn"
        title={nums.length ? t("未在查询结果中直接匹配的数值：{{nums}}", { nums: nums.join("、") }) : undefined}
      >
        <Icon name="alert" small />
        {t("解读数字待核对")}
      </Pill>
      {nums.length ? (
        <span className="av-caption av-grounding-warn__nums" role="status">
          {t("待核对数值：{{nums}}", { nums: nums.join("、") })}
        </span>
      ) : null}
    </span>
  );
}

export function AnswerFeedback({ answer, onUpdated }: { answer: Answer; onUpdated?: (a: Answer) => void }) {
  const [rating, setRating] = useState<"up" | "down" | null>(answer.feedback_rating ?? null);
  const [note, setNote] = useState(answer.feedback_note ?? "");
  const [noteOpen, setNoteOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const downBtnRef = useRef<HTMLButtonElement>(null);
  const noteInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    setNoteOpen(false);
    setError(null);
  }, [answer.id]);

  useEffect(() => {
    setRating(answer.feedback_rating ?? null);
    setNote(answer.feedback_note ?? "");
  }, [answer.id, answer.feedback_rating, answer.feedback_note]);

  const closeNote = () => {
    setNoteOpen(false);
    window.setTimeout(() => downBtnRef.current?.focus(), 0);
  };

  useOverlay(noteOpen, closeNote);

  useEffect(() => {
    if (noteOpen) noteInputRef.current?.focus();
  }, [noteOpen]);

  const submit = useAction(
    (vars: { rating: "up" | "down" | null; note?: string | null }) =>
      unwrap(api.POST("/api/answers/{answer_id}/feedback", {
        params: { path: { answer_id: answer.id } },
        body: { rating: vars.rating, note: vars.note ?? null },
      })),
    ["thread", "threads", "analysis-run", "analysis-conversation"],
  );

  if (answer.status === "clarify") return null;

  const sendFeedback = (nextRating: "up" | "down" | null, nextNote?: string | null, keepOpen = false) => {
    setError(null);
    submit.mutate(
      { rating: nextRating, note: nextNote ?? null },
      {
        onSuccess: (updated) => {
          setRating(updated.feedback_rating ?? nextRating);
          setNote(updated.feedback_note ?? "");
          if (!keepOpen) setNoteOpen(false);
          onUpdated?.(updated);
        },
        onError: (err) => setError(errorText(err)),
      },
    );
  };

  return (
    <div className="av-feedback" onClick={(e) => e.stopPropagation()}>
      <div className="av-feedback__btns" role="group" aria-label={t("回答反馈")}>
        <button
          type="button"
          className={`av-btn av-btn--ghost av-btn--sm av-feedback__btn${rating === "up" ? " is-up" : ""}`}
          aria-pressed={rating === "up"}
          aria-label={t("有帮助")}
          title={t("有帮助")}
          disabled={submit.isPending}
          onClick={() => sendFeedback(rating === "up" ? null : "up", null)}
        >
          <Icon name="thumbUp" small />
          <span>{t("有帮助")}</span>
        </button>
        <button
          ref={downBtnRef}
          type="button"
          className={`av-btn av-btn--ghost av-btn--sm av-feedback__btn${rating === "down" ? " is-down" : ""}`}
          aria-pressed={rating === "down"}
          aria-expanded={noteOpen}
          aria-label={t("待改进")}
          title={t("待改进")}
          disabled={submit.isPending}
          onClick={() => {
            if (noteOpen) {
              closeNote();
            } else if (rating === "down") {
              sendFeedback(null, null);
            } else {
              setNoteOpen(true);
              sendFeedback("down", note.trim() || null, true);
            }
          }}
        >
          <Icon name="thumbDown" small />
          <span>{t("待改进")}</span>
        </button>
      </div>
      {noteOpen ? (
        <form
          className="av-feedback__reason"
          role="region"
          aria-label={t("反馈说明")}
          onSubmit={(e) => {
            e.preventDefault();
            sendFeedback("down", note.trim() || null, false);
          }}
        >
          <input
            ref={noteInputRef}
            className="av-input av-feedback__input"
            maxLength={1000}
            value={note}
            disabled={submit.isPending}
            aria-label={t("说明哪里不准确或缺少什么")}
            placeholder={t("可选：说明哪里不准确或缺少什么…")}
            onChange={(e) => setNote(e.target.value)}
          />
          <button type="submit" className="av-btn av-btn--secondary av-btn--sm" disabled={submit.isPending}>
            {t("提交说明")}
          </button>
          <button type="button" className="av-btn av-btn--ghost av-btn--sm" onClick={closeNote}>
            {t("关闭")}
          </button>
        </form>
      ) : null}
      {error ? <span className="av-caption av-feedback__err" role="alert">{error}</span> : null}
    </div>
  );
}

export function SaveAsVqrButton({ answer, runId }: { answer: Answer; runId?: string }) {
  const roles = useRoles();
  const [open, setOpen] = useState(false);
  const [savedId, setSavedId] = useState<string | null>(null);

  const canCreate = roles.has("steward") || roles.has("reviewer") || roles.has("admin");
  if (!canCreate || answer.status !== "answered" || !answer.sql) return null;

  const intent = (answer.intent ?? {}) as { metric_codes?: string[]; metric_code?: string };
  const metricCodes = Array.isArray(intent.metric_codes) && intent.metric_codes.length
    ? intent.metric_codes
    : intent.metric_code ? [intent.metric_code] : [];

  return (
    <>
      {savedId ? (
        <Link
          to={`/catalog/queries?id=${encodeURIComponent(savedId)}`}
          className="av-pill av-pill--ok"
          onClick={(e) => e.stopPropagation()}
        >
          <Icon name="check" small />
          {t("已存为标准查询")}
        </Link>
      ) : (
        <button
          type="button"
          className="av-btn av-btn--ghost av-btn--sm"
          onClick={(e) => { e.stopPropagation(); setOpen(true); }}
        >
          <Icon name="check" small />
          {t("存为标准查询")}
        </button>
      )}
      {open ? (
        <VqrEditorDrawer
          prefill={{
            title: answer.question.slice(0, 120),
            question: answer.question,
            sql_text: formatSql(answer.sql),
            metric_codes: metricCodes,
            source_answer_id: answer.id,
            source_run_id: runId,
          }}
          onClose={() => setOpen(false)}
          onSaved={(vq) => setSavedId(vq.id)}
        />
      ) : null}
    </>
  );
}
