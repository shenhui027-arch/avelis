// 口径与 Apache Ossie 互通（ADR 0017 §1–2，锁定规范提交 88e0011 / 0.2.0.dev0）：
// 按业务域导出已发布口径；导入先做三层校验（结构 → 模型内部 → 对照已采集资产），通过后只生成草稿，仍走复核发布。
import { useRef, useState } from "react";
import { Link } from "react-router";

import { token, type Schemas } from "../../api/client";
import { errorText, useAction, useDomains } from "../../api/hooks";
import { Banner, Drawer, Hint, Icon, Pill, Select, useMessage } from "../../components/ui";
import { currentLanguage, t } from "../../i18n";
import { useMe, useRoles } from "../../layout/Shell";

type Report = Schemas["OssieImportReport"];
const LAYER_LABEL: Record<string, string> = { structure: t("结构"), model: t("模型内部"), catalog: t("对照目录"), permission: t("权限") };
const ACTION_LABEL: Record<string, string> = { created: t("新建口径"), new_version: t("新版本"), skipped: t("跳过"), failed: t("失败") };

/** 下载一个业务域的 Ossie 语义模型文件（只含当前用户可见的已发布口径）。 */
export async function downloadOssie(domainId: string): Promise<void> {
  const resp = await fetch(`/api/interchange/ossie/export?${new URLSearchParams({ domain_id: domainId })}`, {
    headers: { Authorization: `Bearer ${token.get() ?? ""}`, "Accept-Language": currentLanguage() } });
  if (!resp.ok) throw new Error((await resp.json().catch(() => ({}))).detail ?? t("导出失败（{{status}}）", { status: resp.status }));
  const disposition = resp.headers.get("Content-Disposition") ?? "";
  const encoded = /filename\*=UTF-8''([^;]+)/.exec(disposition)?.[1];
  const name = encoded ? decodeURIComponent(encoded) : /filename="([^"]+)"/.exec(disposition)?.[1] ?? "domain.ossie.yaml";
  const url = URL.createObjectURL(await resp.blob());
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

export function OssieExportButton({ domainId }: { domainId: string }) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useMessage();
  return <>
    <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={busy} title={t("导出本业务域已发布的口径（Apache Ossie 格式）")}
      onClick={async () => {
        setBusy(true);
        try { await downloadOssie(domainId); } catch (e) { setMessage({ ok: false, text: errorText(e) }); } finally { setBusy(false); }
      }}><Icon name="download" small />{busy ? t("导出中…") : t("导出 Ossie")}</button>
    {message ? <div className="av-ossie__toast"><Banner message={message} /></div> : null}
  </>;
}

async function postImport(file: File, domainId: string, dryRun: boolean): Promise<{ status: number; report: Report }> {
  const form = new FormData();
  form.append("file", file);
  form.append("domain_id", domainId);
  form.append("dry_run", String(dryRun));
  const resp = await fetch("/api/interchange/ossie/import", { method: "POST", body: form,
    headers: { Authorization: `Bearer ${token.get() ?? ""}`, "Accept-Language": currentLanguage() } });
  const body = await resp.json().catch(() => ({}));
  // 400 / 403 也返回完整的校验报告；其他错误只有 detail
  if (!resp.ok && !Array.isArray(body.errors)) throw new Error(body.detail ?? t("导入失败（{{status}}）", { status: resp.status }));
  return { status: resp.status, report: body as Report };
}

export function OssieImportDrawer({ onClose }: { onClose: () => void }) {
  const roles = useRoles();
  const me = useMe();
  const domains = useDomains();
  // 能导入的业务域：管理员全部；否则是自己担任负责人或维护者的域（与后端 require_domain_maintainer 一致）
  const targets = (domains.data ?? []).filter((d) => roles.has("admin")
    || d.members.some((m) => m.user_id === me.data?.id && (m.role === "owner" || m.role === "maintainer")));
  const [domainId, setDomainId] = useState("");
  const [file, setFile] = useState<File | null>(null);
  const [checked, setChecked] = useState<{ file: File; domainId: string; report: Report } | null>(null);
  const [done, setDone] = useState<Report | null>(null);
  const [message, setMessage] = useMessage();
  const input = useRef<HTMLInputElement>(null);
  const target = domainId || targets[0]?.id || "";
  const fresh = checked && checked.file === file && checked.domainId === target;
  const run = useAction((dryRun: boolean) => postImport(file!, target, dryRun), dryRunInvalidates);

  const validate = () => run.mutate(true, {
    onSuccess: (out) => { setChecked({ file: file!, domainId: target, report: out.report }); setDone(null); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }) });
  const commit = () => run.mutate(false, {
    onSuccess: (out) => { if (out.report.ok) setDone(out.report); else setChecked({ file: file!, domainId: target, report: out.report }); },
    onError: (e) => setMessage({ ok: false, text: errorText(e) }) });
  const report = done ?? (fresh ? checked.report : null);

  return <Drawer title={t("导入 Ossie 语义模型")} width={560} onClose={() => { if (!run.isPending) onClose(); }} footer={<div className="av-actions">
    <button type="button" className="av-btn av-btn--ghost" disabled={run.isPending} onClick={onClose}>{done ? t("关闭") : t("取消")}</button>
    {done ? null : <>
      <button type="button" className="av-btn av-btn--secondary" disabled={!file || !target || run.isPending} onClick={validate}>
        {run.isPending && run.variables === true ? t("校验中…") : t("校验")}</button>
      <button type="button" className="av-btn av-btn--primary" disabled={!fresh || !checked?.report.ok || run.isPending}
        title={!fresh ? t("先校验通过才能导入") : undefined} onClick={commit}>
        {run.isPending && run.variables === false ? t("导入中…") : t("导入为草稿")}</button>
    </>}
  </div>}>
    <div className="av-ossie">
      <p className="av-caption">{t("支持 Apache Ossie 0.2.0.dev0（提交 48da55a）的 YAML 或 JSON，也能读取旧版（提交 88e0011）的文件。导入只生成草稿版本，必须经过复核才能发布；关联路径不会自动创建。")}</p>
      <div className="av-field"><span className="av-label">{t("目标业务域")}</span>
        {targets.length ? <Select label={t("目标业务域")} value={target} disabled={!!done} onChange={setDomainId}>
          {targets.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}</Select>
          : <span className="av-caption">{t("你没有可导入的业务域（需要业务域负责人或维护者身份）")}</span>}</div>
      <div className="av-field"><span className="av-label">{t("文件")} <Hint text={t("不超过 5 MiB")} /></span>
        <div className="av-ossie__file">
          <button type="button" className="av-btn av-btn--secondary av-btn--sm" disabled={!!done} onClick={() => input.current?.click()}>
            <Icon name="upload" small />{t("选择文件")}</button>
          <span className="av-mono av-caption">{file ? `${file.name} · ${(file.size / 1024).toFixed(1)} KB` : t("未选择")}</span>
          <input ref={input} type="file" accept=".yaml,.yml,.json,application/yaml,application/json" hidden
            onChange={(e) => { setFile(e.target.files?.[0] ?? null); setDone(null); e.target.value = ""; }} />
        </div></div>
      <Banner message={message} />
      {report ? <OssieReport report={report} committed={!!done} /> : null}
    </div>
  </Drawer>;
}

const dryRunInvalidates = ["metrics", "metric-version"];

function OssieReport({ report, committed }: { report: Report; committed: boolean }) {
  const counts = report.counts ?? {};
  return <div className="av-ossie__report">
    <div className="av-ossie__summary">
      {report.ok ? <Pill tone="ok">{committed ? t("已导入") : t("校验通过")}</Pill> : <Pill tone="danger">{t("校验未通过，未写入任何内容")}</Pill>}
      {committed ? <span className="av-caption">{t("新建 {{a}} · 新版本 {{b}} · 跳过 {{c}}", { a: counts.created ?? 0, b: counts.new_version ?? 0, c: counts.skipped ?? 0 })}</span> : null}
    </div>
    {report.errors?.length ? <section><h3>{t("错误")} <span className="av-caption">{report.errors.length}</span></h3>
      <table className="av-table"><thead><tr><th style={{ paddingLeft: 12 }}>{t("层级")}</th><th>{t("位置")}</th><th>{t("原因")}</th></tr></thead>
        <tbody>{report.errors.map((e, i) => <tr key={i}><td style={{ paddingLeft: 12 }}>{LAYER_LABEL[e.layer] ?? e.layer}</td>
          <td className="av-mono av-ossie__path">{e.path}</td><td style={{ whiteSpace: "normal" }}>{e.reason}</td></tr>)}</tbody></table></section> : null}
    {report.warnings?.length ? <section><h3>{t("提醒")} <span className="av-caption">{report.warnings.length}</span></h3>
      <ul className="av-ossie__warnings">{report.warnings.map((w, i) => <li key={i}><b>{w.metric}</b> <span className="av-mono av-caption">{w.path}</span><br />{w.reason}</li>)}</ul></section> : null}
    {report.results?.length ? <section><h3>{committed ? t("结果") : t("将执行")}</h3>
      <table className="av-table"><thead><tr><th style={{ paddingLeft: 12 }}>{t("口径")}</th><th>{t("操作")}</th><th>{t("说明")}</th></tr></thead>
        <tbody>{report.results.map((r) => <tr key={r.name}><td style={{ paddingLeft: 12 }} className="av-mono">
          {r.metric_id && committed ? <Link to={`/catalog/metrics/${r.metric_id}`}>{r.name}</Link> : r.name}{r.version_no ? ` V${r.version_no}` : ""}</td>
          <td>{!committed && r.action === "skipped"
            // 预检不写入，后端把每条记为 skipped；说明里写了正式导入时会做什么
            ? <Pill tone="ok">{t("可导入")}</Pill>
            : <Pill tone={r.action === "failed" ? "danger" : r.action === "skipped" ? undefined : "ok"}>{ACTION_LABEL[r.action] ?? r.action}</Pill>}</td>
          <td className="av-caption" style={{ whiteSpace: "normal" }}>{r.reason ?? (committed ? t("已生成草稿，提交复核后才能发布") : "")}</td></tr>)}</tbody></table></section> : null}
  </div>;
}
