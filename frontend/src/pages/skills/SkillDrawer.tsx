import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import { api, unwrap, type Schemas } from "../../api/client";
import { errorText, useTimeZone } from "../../api/hooks";
import {
  Banner, ConfirmDialog, Count, Drawer, Icon, Pill, QueryState, Status, Switch, Tabs, Tile, useMessage,
} from "../../components/ui";
import { formatShortTime, formatTime } from "../../format";
import { t } from "../../i18n";
import { formatSkillBytes, skillFetch, type UploadPreset } from "./UploadDrawer";

type SkillDetail = Schemas["SkillDetailOut"];
type SkillVersion = Schemas["SkillVersionOut"];
type SkillFile = Schemas["SkillFileOut"];
type DetailTab = "readme" | "files" | "versions";

const MD_COMPONENTS: Components = {
  a: ({ href, children }) =>
    href && /^(https?:|mailto:)/i.test(href)
      ? <a href={href} target="_blank" rel="noopener noreferrer">{children}</a>
      : <span>{children}</span>,
  img: ({ alt }) => <span className="av-skill-md__img-alt">{alt || t("图片")}</span>,
};

type DirNode = { name: string; path: string; dirs: DirNode[]; files: SkillFile[] };

function buildFileTree(files: SkillFile[]): DirNode {
  const root: DirNode = { name: "", path: "", dirs: [], files: [] };
  const map = new Map<string, DirNode>([["", root]]);
  const ensureDir = (dirPath: string): DirNode => {
    const existing = map.get(dirPath);
    if (existing) return existing;
    const slash = dirPath.lastIndexOf("/");
    const parentPath = slash >= 0 ? dirPath.slice(0, slash) : "";
    const name = slash >= 0 ? dirPath.slice(slash + 1) : dirPath;
    const parent = ensureDir(parentPath);
    const node: DirNode = { name, path: dirPath, dirs: [], files: [] };
    parent.dirs.push(node);
    map.set(dirPath, node);
    return node;
  };
  for (const f of files) {
    const slash = f.path.lastIndexOf("/");
    const dir = ensureDir(slash >= 0 ? f.path.slice(0, slash) : "");
    dir.files.push(f);
  }
  const sortNode = (node: DirNode, isRoot: boolean) => {
    node.dirs.sort((a, b) => a.name.localeCompare(b.name));
    node.files.sort((a, b) => {
      if (isRoot && a.path === "SKILL.md") return -1;
      if (isRoot && b.path === "SKILL.md") return 1;
      return a.path.localeCompare(b.path);
    });
    for (const child of node.dirs) sortNode(child, false);
  };
  sortNode(root, true);
  return root;
}

function stripFrontmatter(raw: string): string {
  return raw.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
}

function formatFrontmatterValue(key: string, val: unknown): string | null {
  if (val == null) return null;
  if (key === "metadata") {
    if (typeof val !== "object" || Array.isArray(val)) return null;
    const entries = Object.entries(val as Record<string, unknown>);
    if (!entries.length) return null;
    return entries.map(([k, v]) => `${k}: ${JSON.stringify(v)}`).join("\n");
  }
  const str = String(val).trim();
  return str ? str : null;
}

function FrontmatterBlock({ frontmatter }: { frontmatter: Record<string, unknown> }) {
  const keys = ["name", "description", "license", "compatibility", "metadata"] as const;
  const rows = keys
    .map((k) => [k, formatFrontmatterValue(k, frontmatter[k])] as const)
    .filter((pair): pair is [typeof keys[number], string] => pair[1] !== null);
  if (!rows.length) return null;
  return (
    <dl className="av-skill-front">
      {rows.map(([k, v]) => (
        <div key={k} className="av-skill-front__row">
          <dt>{k}</dt>
          <dd className={k === "description" || k === "compatibility" ? "is-prose" : undefined}>{v}</dd>
        </div>
      ))}
    </dl>
  );
}

function SkillStatusPill({ status }: { status: string }) {
  if (status === "listed") return <Pill tone="ok">{t("已上架")}</Pill>;
  if (status === "pending") return <Pill tone="warn">{t("待审核")}</Pill>;
  return <Pill>{t("已下架")}</Pill>;
}

function VersionStatus({ versionStatus, skillStatus }: { versionStatus: string; skillStatus: string }) {
  if (versionStatus === "pending") return <Status value="open" label={t("待审核")} />;
  if (versionStatus === "approved") {
    return skillStatus === "delisted"
      ? <Status value="archived" label={t("已下架")} />
      : <Status value="approved" label={t("已上架")} />;
  }
  if (versionStatus === "rejected") return <Status value="rejected" label={t("已驳回")} />;
  if (versionStatus === "withdrawn") return <Status value="archived" label={t("已撤回")} />;
  return <Status value="superseded" label={t("已被替换")} />;
}

function FileTreeBranch({ node, selectedPath, onSelect }: {
  node: DirNode; selectedPath: string | null; onSelect: (path: string) => void;
}) {
  return (
    <>
      {node.files.map((f) => {
        const slash = f.path.lastIndexOf("/");
        const base = slash >= 0 ? f.path.slice(slash + 1) : f.path;
        return (
          <button
            key={f.path}
            type="button"
            className={`av-skill-tree__file${selectedPath === f.path ? " is-active" : ""}`}
            title={f.path}
            onClick={() => onSelect(f.path)}
          >
            <Icon name={f.kind === "skill_md" ? "skill" : f.kind === "script" ? "lock" : "audit"} small />
            <span className="av-skill-tree__name">{base}</span>
            <span className="av-skills__spacer" />
            {f.kind === "script" ? (
              <span className="av-tag av-skill-script-tag"><Icon name="lock" small />{t("脚本不执行")}</span>
            ) : null}
            <small className="av-skill-tree__size">{formatSkillBytes(f.size)}</small>
          </button>
        );
      })}
      {node.dirs.map((dir) => (
        <details key={dir.path} className="av-disclosure av-skill-tree__dir" open>
          <summary><span className="av-mono">{dir.name}/</span></summary>
          <div className="av-skill-tree__children">
            <FileTreeBranch node={dir} selectedPath={selectedPath} onSelect={onSelect} />
          </div>
        </details>
      ))}
    </>
  );
}

async function downloadVersionZip(skillId: string, skillName: string, version: number) {
  const resp = await skillFetch(`/api/skills/${encodeURIComponent(skillId)}/versions/${version}/download`);
  const disposition = resp.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/.exec(disposition)?.[1];
  const filename = encoded ? decodeURIComponent(encoded)
    : /filename="([^"]+)"/.exec(disposition)?.[1] ?? `${skillName}-v${version}.zip`;
  const url = URL.createObjectURL(await resp.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  window.setTimeout(() => URL.revokeObjectURL(url), 0);
}

/** 管理员驳回技能版本对话框（列表行与详情抽屉共用）。 */
export function RejectSkillDialog({
  name,
  version,
  versionId,
  onCancel,
  onRejected,
  busyRef,
}: {
  name: string;
  version: number;
  versionId: string;
  onCancel: () => void;
  onRejected: () => void;
  busyRef?: { current: boolean };
}) {
  const [reason, setReason] = useState("");
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const internalBusyRef = useRef(false);

  useEffect(() => {
    const id = window.setTimeout(() => inputRef.current?.focus(), 0);
    return () => window.clearTimeout(id);
  }, []);

  return (
    <ConfirmDialog
      title={t("驳回 {{name}} v{{version}}", { name, version })}
      confirm={t("驳回")}
      danger
      onCancel={() => {
        if (internalBusyRef.current) return;
        onCancel();
      }}
      onConfirm={async () => {
        const trimmed = reason.trim();
        if (!trimmed) throw new Error(t("请填写驳回理由"));
        internalBusyRef.current = true;
        if (busyRef) busyRef.current = true;
        try {
          await unwrap(api.POST("/api/skills/versions/{version_id}/reject", {
            params: { path: { version_id: versionId } },
            body: { reason: trimmed },
          }));
          onRejected();
        } finally {
          internalBusyRef.current = false;
          if (busyRef) busyRef.current = false;
        }
      }}
    >
      <label className="av-field">
        <span className="av-label">{t("驳回理由")}</span>
        <textarea
          ref={inputRef}
          className="av-textarea"
          rows={3}
          maxLength={500}
          value={reason}
          placeholder={t("请填写驳回理由（1–500 字）")}
          onChange={(e) => setReason(e.target.value)}
          onKeyDown={(e) => {
            if ((e.nativeEvent.isComposing || e.keyCode === 229) && e.key === "Escape") {
              e.stopPropagation();
            }
          }}
        />
      </label>
    </ConfirmDialog>
  );
}

export function SkillDrawer({
  skillId,
  isAdmin,
  preferPending,
  toggling,
  onToggleEnabled,
  onClose,
  onUploadNewVersion,
  onInvalidate,
  onPageMessage,
}: {
  skillId: string;
  isAdmin: boolean;
  preferPending?: boolean;
  toggling?: boolean;
  onToggleEnabled: (skillId: string, enabled: boolean, onFail?: (msg: string) => void) => void;
  onClose: () => void;
  onUploadNewVersion: (preset: UploadPreset) => void;
  onInvalidate: () => void;
  onPageMessage: (msg: { ok: boolean; text: string }) => void;
}) {
  const tz = useTimeZone();
  const [tab, setTab] = useState<DetailTab>("readme");
  const [selectedVersionNo, setSelectedVersionNo] = useState<number | null>(null);
  const [selectedPath, setSelectedPath] = useState<string | null>(null);
  const [downloading, setDownloading] = useState(false);
  const [approving, setApproving] = useState(false);
  const [confirmingDelist, setConfirmingDelist] = useState(false);
  const [rejecting, setRejecting] = useState(false);
  const delistingRef = useRef(false);
  const rejectingBusyRef = useRef(false);
  const [message, setMessage] = useMessage();

  useEffect(() => {
    setTab("readme");
    setSelectedVersionNo(null);
    setSelectedPath(null);
    setMessage(null);
  }, [skillId, setMessage]);

  // 确认框（下架 / 驳回）打开时，在捕获阶段拦截 Esc 只关闭确认框，避免触发底层 Drawer 退场关闭
  useEffect(() => {
    if (!confirmingDelist && !rejecting) return;
    const onKeyDown = (e: globalThis.KeyboardEvent) => {
      if (e.isComposing || e.keyCode === 229 || e.key !== "Escape") return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (delistingRef.current || rejectingBusyRef.current) return;
      if (rejecting) setRejecting(false);
      else if (confirmingDelist) setConfirmingDelist(false);
    };
    window.addEventListener("keydown", onKeyDown, true);
    return () => window.removeEventListener("keydown", onKeyDown, true);
  }, [confirmingDelist, rejecting]);

  const detailQuery = useQuery({
    queryKey: ["skills", "detail", skillId],
    queryFn: () => unwrap(api.GET("/api/skills/{skill_id}", { params: { path: { skill_id: skillId } } })),
  });

  const detail: SkillDetail | undefined = detailQuery.data;

  const activeVersion: SkillVersion | undefined = useMemo(() => {
    if (!detail) return undefined;
    if (selectedVersionNo !== null) {
      const found = detail.versions.find((v) => v.version === selectedVersionNo);
      if (found) return found;
    }
    if (preferPending) {
      const pending = detail.versions.find((v) => v.status === "pending");
      if (pending) return pending;
    }
    return detail.shown_version;
  }, [detail, selectedVersionNo, preferPending]);

  const isOtherVersion = !!detail && !!activeVersion && activeVersion.id !== detail.shown_version.id;

  const altReadmeQuery = useQuery({
    queryKey: ["skills", "file", skillId, activeVersion?.version, "SKILL.md"],
    enabled: tab === "readme" && isOtherVersion,
    queryFn: () => unwrap(api.GET("/api/skills/{skill_id}/versions/{version}/files", {
      params: { path: { skill_id: skillId, version: activeVersion!.version }, query: { path: "SKILL.md" } },
    })),
  });

  const fileQuery = useQuery({
    queryKey: ["skills", "file", skillId, activeVersion?.version, selectedPath],
    enabled: tab === "files" && !!activeVersion && !!selectedPath,
    queryFn: () => unwrap(api.GET("/api/skills/{skill_id}/versions/{version}/files", {
      params: { path: { skill_id: skillId, version: activeVersion!.version }, query: { path: selectedPath! } },
    })),
  });

  const tree = useMemo(() => buildFileTree(activeVersion?.files ?? []), [activeVersion?.files]);

  async function handleDownload() {
    if (!detail || !activeVersion || downloading) return;
    setDownloading(true);
    setMessage(null);
    try {
      await downloadVersionZip(detail.id, detail.name, activeVersion.version);
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setDownloading(false);
    }
  }

  async function handleApprove() {
    if (!activeVersion || approving) return;
    setApproving(true);
    setMessage(null);
    try {
      await unwrap(api.POST("/api/skills/versions/{version_id}/approve", {
        params: { path: { version_id: activeVersion.id } },
      }));
      onInvalidate();
      setSelectedVersionNo(null);
      setMessage({ ok: true, text: t("已通过并上架") });
      onPageMessage({ ok: true, text: t("已通过并上架") });
    } catch (e) {
      setMessage({ ok: false, text: errorText(e) });
    } finally {
      setApproving(false);
    }
  }

  const adminPending = isAdmin && activeVersion?.status === "pending";
  const hasPendingVersion = !!detail && (detail.latest_status === "pending" || detail.versions.some((v) => v.status === "pending"));
  const pendingTip = hasPendingVersion ? t("该技能已有待审核的版本") : undefined;

  const uploadBtn = detail ? (
    <button
      type="button"
      className="av-btn av-btn--secondary"
      disabled={hasPendingVersion}
      onClick={() => onUploadNewVersion({ id: detail.id, name: detail.name, version: detail.version ?? null })}
    >
      <Icon name="upload" />{t("上传新版本")}
    </button>
  ) : null;

  const footer = detail && activeVersion ? (
    <>
      <button type="button" className="av-btn av-btn--ghost" disabled={downloading} onClick={handleDownload}>
        <Icon name="download" />{downloading ? t("下载中…") : t("下载")}
      </button>
      <span className="av-skills__spacer" />
      {adminPending ? (
        <>
          <button type="button" className="av-btn av-btn--secondary" disabled={approving} onClick={() => setRejecting(true)}>
            {t("驳回")}
          </button>
          <button type="button" className="av-btn av-btn--primary" disabled={approving} onClick={handleApprove}>
            {approving ? t("处理中…") : t("通过并上架")}
          </button>
        </>
      ) : detail.can_manage ? (
        <>
          {detail.status === "listed" ? (
            <button type="button" className="av-btn av-btn--ghost av-skill-delist-btn" onClick={() => setConfirmingDelist(true)}>
              {t("下架")}
            </button>
          ) : null}
          {hasPendingVersion ? (
            <span className="av-skill-tip-wrap" data-tip={pendingTip} tabIndex={0}>{uploadBtn}</span>
          ) : uploadBtn}
        </>
      ) : null}
    </>
  ) : undefined;

  return (
    <>
      <Drawer
        title={t("技能详情")}
        width={600}
        onClose={() => {
          if (confirmingDelist || rejecting || approving) return;
          onClose();
        }}
        footer={footer}
      >
        <QueryState query={detailQuery}>
          {(d) => {
            const ver = activeVersion ?? d.shown_version;
            const fm = (ver.frontmatter ?? {}) as Record<string, unknown>;
            const license = typeof fm.license === "string" && fm.license.trim() ? fm.license.trim() : null;
            const scriptFiles = ver.files.filter((f) => f.kind === "script");
            const scriptCount = scriptFiles.length || (ver.has_scripts ? 1 : 0);
            const selectedFile = selectedPath ? ver.files.find((f) => f.path === selectedPath) : undefined;

            return (
              <>
                <div className="av-skill-dhead">
                  <div className="av-skill-dhead__row">
                    <Tile tone="ask" icon="skill" />
                    <span className="av-skill-dhead__name">{d.name}</span>
                    <SkillStatusPill status={d.status} />
                    <span className="av-tag">v{ver.version}</span>
                  </div>
                  <div className="av-skill-stats">
                    <span className="av-skill-stats__user">
                      <span className="av-avatar av-skill-avatar" aria-hidden>{ver.uploaded_by.display_name.slice(0, 1)}</span>
                      <span>{t("上传")} <b>{ver.uploaded_by.display_name}</b></span>
                    </span>
                    {ver.reviewed_by ? (
                      <span>
                        {t("审核", { context: "by" })} <b>{ver.reviewed_by.display_name}</b>
                        {ver.reviewed_at ? ` · ${formatShortTime(ver.reviewed_at, tz)}` : ""}
                      </span>
                    ) : null}
                    <span>{t("{{count}} 人启用", { count: d.enabled_users })}</span>
                    {license ? <span>{t("许可")} <b>{license}</b></span> : null}
                  </div>
                </div>

                <div className="av-skill-enable">
                  <div className="av-skill-enable__text">
                    <span>{t("为我启用")}</span>
                    <small>{t("启用后，问数与分析在需要时加载此技能；只对你本人生效")}</small>
                  </div>
                  <Switch
                    checked={d.enabled}
                    disabled={d.status !== "listed" || toggling}
                    label={t("为我启用 {{name}}", { name: d.name })}
                    onChange={(next) => {
                      setMessage(null);
                      onToggleEnabled(d.id, next, (errText) => setMessage({ ok: false, text: errText }));
                    }}
                  />
                </div>

                <Banner message={message} />

                {ver.has_scripts || scriptCount > 0 ? (
                  <div className="av-banner av-banner--warn">
                    {t("包含 {{count}} 个脚本：只保存，不会执行", { count: scriptCount })}
                  </div>
                ) : null}

                <Tabs
                  value={tab}
                  onChange={setTab}
                  options={[
                    { value: "readme", label: t("说明") },
                    { value: "files", label: <>{t("文件")} <Count n={ver.files.length} /></> },
                    { value: "versions", label: <>{t("版本")} <Count n={d.versions.length} /></> },
                  ]}
                />

                {ver.id !== d.shown_version.id && tab !== "versions" ? (
                  <div className="av-skill-ver-bar">
                    <span className="av-tag">{t("当前查看 v{{version}}", { version: ver.version })}</span>
                    <VersionStatus versionStatus={ver.status} skillStatus={d.status} />
                  </div>
                ) : null}

                {tab === "readme" ? (
                  <div className="av-skill-readme">
                    <FrontmatterBlock frontmatter={fm} />
                    {ver.id !== d.shown_version.id ? (
                      <QueryState query={altReadmeQuery} compact rows={3}>
                        {(fc) => (
                          <>
                            {fc.truncated ? (
                              <div className="av-banner av-banner--info">{t("文件内容较长，仅显示前 256 KB")}</div>
                            ) : null}
                            <div className="av-skill-md">
                              <Markdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>
                                {stripFrontmatter(fc.content ?? "")}
                              </Markdown>
                            </div>
                          </>
                        )}
                      </QueryState>
                    ) : (
                      <div className="av-skill-md">
                        <Markdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>{d.body}</Markdown>
                      </div>
                    )}
                  </div>
                ) : null}

                {tab === "files" ? (
                  <div className="av-skill-files-pane">
                    <div className="av-skill-tree">
                      <FileTreeBranch node={tree} selectedPath={selectedPath} onSelect={setSelectedPath} />
                    </div>
                    {selectedPath ? (
                      <div className="av-skill-preview">
                        <div className="av-skill-preview__hd">
                          <span className="av-mono av-skill-preview__path">{selectedPath}</span>
                          <span className="av-skills__spacer" />
                          {selectedFile?.kind === "script" ? (
                            <span className="av-tag av-skill-script-tag"><Icon name="lock" small />{t("脚本不执行")}</span>
                          ) : null}
                          {selectedFile ? <small className="av-caption">{formatSkillBytes(selectedFile.size)}</small> : null}
                        </div>
                        <QueryState query={fileQuery} compact rows={3}>
                          {(fc) => (
                            <>
                              {fc.truncated ? (
                                <div className="av-banner av-banner--info">{t("文件内容较长，仅显示前 256 KB")}</div>
                              ) : null}
                              {fc.binary ? (
                                <div className="av-skill-preview__binary">{t("二进制文件，不提供预览")}</div>
                              ) : (
                                <pre className="av-code av-skill-preview__code">{fc.content ?? ""}</pre>
                              )}
                            </>
                          )}
                        </QueryState>
                      </div>
                    ) : null}
                  </div>
                ) : null}

                {tab === "versions" ? (
                  <div className="av-skill-versions">
                    {d.versions.map((v) => {
                      const isCurrent = v.id === ver.id;
                      return (
                        <div key={v.id} className={`av-skill-ver${isCurrent ? " is-current" : ""}`}>
                          <div className="av-skill-ver__hd">
                            <span className="av-tag">v{v.version}</span>
                            <VersionStatus versionStatus={v.status} skillStatus={d.status} />
                            {v.has_scripts ? (
                              <span className="av-tag av-skill-script-tag"><Icon name="lock" small />{t("脚本不执行")}</span>
                            ) : null}
                            <span className="av-skills__spacer" />
                            {isCurrent ? <span className="av-caption">{t("当前查看")}</span> : null}
                            <button
                              type="button"
                              className="av-btn av-btn--ghost av-btn--sm"
                              onClick={() => {
                                setSelectedVersionNo(v.version);
                                setSelectedPath(null);
                                setTab("files");
                              }}
                            >
                              {t("查看文件")}
                            </button>
                          </div>
                          {v.note ? <p className="av-skill-ver__note">{v.note}</p> : null}
                          <div className="av-skill-ver__meta">
                            <span>{t("上传")} {v.uploaded_by.display_name} · {formatTime(v.uploaded_at, tz)}</span>
                            {v.reviewed_by ? (
                              <span>
                                {t("审核", { context: "by" })} {v.reviewed_by.display_name}
                                {v.reviewed_at ? ` · ${formatTime(v.reviewed_at, tz)}` : ""}
                              </span>
                            ) : null}
                          </div>
                          {v.review_note ? (
                            <div className="av-skill-ver__reject">{t("驳回理由：{{reason}}", { reason: v.review_note })}</div>
                          ) : null}
                        </div>
                      );
                    })}
                  </div>
                ) : null}
              </>
            );
          }}
        </QueryState>
      </Drawer>

      {confirmingDelist && detail ? (
        <ConfirmDialog
          title={t("下架技能 {{name}}？", { name: detail.name })}
          body={t("下架后不在市场显示，已启用的人也不再加载。")}
          confirm={t("下架")}
          danger
          onCancel={() => {
            if (delistingRef.current) return;
            setConfirmingDelist(false);
          }}
          onConfirm={async () => {
            delistingRef.current = true;
            try {
              await unwrap(api.POST("/api/skills/{skill_id}/delist", {
                params: { path: { skill_id: detail.id } },
              }));
              onInvalidate();
              setConfirmingDelist(false);
              setMessage({ ok: true, text: t("已下架") });
              onPageMessage({ ok: true, text: t("已下架") });
            } finally {
              delistingRef.current = false;
            }
          }}
        />
      ) : null}

      {rejecting && detail && activeVersion ? (
        <RejectSkillDialog
          name={detail.name}
          version={activeVersion.version}
          versionId={activeVersion.id}
          busyRef={rejectingBusyRef}
          onCancel={() => setRejecting(false)}
          onRejected={() => {
            onInvalidate();
            setRejecting(false);
            setMessage({ ok: true, text: t("已驳回") });
            onPageMessage({ ok: true, text: t("已驳回") });
          }}
        />
      ) : null}
    </>
  );
}
