// 业务域：固定对象标题，分别浏览指标、资产和成员。
import { useEffect, useState, type KeyboardEvent } from "react";
import { Link } from "react-router";

import { api, unwrap } from "../../api/client";
import { errorText, useAction, useAssetSearch, useDomains, useMetrics, useUsers } from "../../api/hooks";
import { useDebounced } from "../../components/AssetPicker";
import { Banner, Crumbs, Drawer, Empty, Icon, Pager, QueryState, Select, Status, useMessage } from "../../components/ui";
import { useMe } from "../../layout/Shell";
import { t } from "../../i18n";
import { OssieExportButton } from "./OssieInterchange";

const ROLE: Record<string, string> = { owner: t("负责人"), maintainer: t("维护者"), viewer: t("可查询") };
const ASSET_PAGE = 100;
const TABS = [{ value: "metrics", label: t("指标") }, { value: "assets", label: t("资产") }, { value: "members", label: t("成员") }] as const;
type DomainTab = typeof TABS[number]["value"];
type MemberEditor = { user_id: string; role: string; name?: string };

export function DomainDetail({ domainId }: { domainId: string }) {
  const domains = useDomains();
  const users = useUsers();
  const metrics = useMetrics();
  const me = useMe();
  const d = domains.data?.find((x) => x.id === domainId);
  const [tab, setTab] = useState<DomainTab>("metrics");
  const [searches, setSearches] = useState({ metrics: "", assets: "", members: "" });
  const [member, setMember] = useState<MemberEditor | null>(null);
  const [message, setMessage] = useMessage();
  const [memberMessage, setMemberMessage] = useMessage();
  const saveMember = useAction((body: { user_id: string; role: string }) => unwrap(api.PUT("/api/domains/{domain_id}/members", {
    params: { path: { domain_id: domainId } }, body,
  })), ["domains", "users"]);
  const isOwner = !!d && (me.data?.id === d.owner_id || !!me.data?.roles.includes("admin"));
  const canMaintain = isOwner || (!!d && d.members.some((m) => m.user_id === me.data?.id && m.role === "maintainer"));
  const [editingExamples, setEditingExamples] = useState(false);
  // 资产按服务端过滤与分页（按页多取一条判断有无下一页）；计数只在未搜索时显示，超过一页显示“N+”
  const [assetOffset, setAssetOffset] = useState(0);
  const assetTerm = useDebounced(searches.assets.trim());
  useEffect(() => setAssetOffset(0), [assetTerm]);
  const assets = useAssetSearch({ domain_id: domainId, q: assetTerm || undefined, limit: ASSET_PAGE + 1, offset: assetOffset });
  const firstPage = useAssetSearch({ domain_id: domainId, limit: ASSET_PAGE + 1 });
  const domainMetrics = (metrics.data ?? []).filter((m) => m.domain_id === domainId);
  const query = searches[tab].trim().toLocaleLowerCase();
  const filteredAssets = (assets.data ?? []).slice(0, ASSET_PAGE);
  const moreAssets = (assets.data?.length ?? 0) > ASSET_PAGE;
  const filteredMetrics = domainMetrics.filter((m) => `${m.name} ${m.code} ${m.owner.display_name}`.toLocaleLowerCase().includes(query));
  const filteredMembers = (d?.members ?? []).filter((m) => `${m.display_name} ${m.username} ${ROLE[m.role] ?? m.role}`.toLocaleLowerCase().includes(query));
  const counts = { metrics: metrics.isSuccess ? domainMetrics.length : "—", assets: firstPage.isSuccess ? (firstPage.data.length > ASSET_PAGE ? `${ASSET_PAGE}+` : firstPage.data.length) : "—", members: d?.members.length ?? "—" };
  const clearSearch = <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setSearches({ ...searches, [tab]: "" })}>{t("清除搜索")}</button>;
  const openMember = (value: MemberEditor) => { setMemberMessage(null); setMember(value); };
  const closeMember = () => { if (!saveMember.isPending) setMember(null); };
  const changeTab = (event: KeyboardEvent<HTMLButtonElement>, index: number) => {
    let next = index;
    if (event.key === "ArrowRight") next = (index + 1) % TABS.length;
    else if (event.key === "ArrowLeft") next = (index + TABS.length - 1) % TABS.length;
    else if (event.key === "Home") next = 0;
    else if (event.key === "End") next = TABS.length - 1;
    else return;
    event.preventDefault();
    setTab(TABS[next].value);
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>("button")[next]?.focus();
  };
  return (
    <QueryState query={domains} isEmpty={(items) => !items.some((item) => item.id === domainId)} emptyText={t("业务域不存在")}
      emptyAction={<Link className="av-btn av-btn--secondary" to="/catalog/domains">{t("返回业务域")}</Link>}>
      {() => d ? (
        <div className="av-domain-detail">
          <header className="av-domain-detail__header">
            <Crumbs back={{ label: t("业务域"), to: "/catalog/domains" }} items={[<span className="av-mono">{d.code}</span>]} />
            <div className="av-pagehd">
              <div className="av-pagehd__title"><h1>{d.name}</h1><span className="av-tag av-mono">{d.code}</span></div>
              <div className="av-actions">
                {canMaintain ? <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => setEditingExamples(true)}>{t("示例问题")}</button> : null}
                <OssieExportButton domainId={d.id} />
                <Link className="av-btn av-btn--secondary av-btn--sm" to={`/lineage?domains=${d.id}`}><Icon name="lineage" small />{t("域血缘")}</Link></div>
            </div>
            <dl className="av-domain-detail__summary">
              <div><dt>{t("负责人")}</dt><dd>{d.members.find((m) => m.user_id === d.owner_id)?.display_name ?? users.data?.find((u) => u.id === d.owner_id)?.display_name ?? (users.isError ? t("加载失败") : users.isPending ? t("加载中…") : t("未提供"))}</dd></div>
              {TABS.map((item) => <div key={item.value}><dt>{item.label}</dt><dd>{counts[item.value]}</dd></div>)}
            </dl>
            {d.description ? <p className="av-domain-detail__description">{d.description}</p> : null}
            {editingExamples ? <ExampleQuestionsEditor domainId={d.id} questions={d.example_questions ?? []}
              onClose={() => setEditingExamples(false)} /> : null}
            <Banner message={message} />
            <div className="av-tabs" role="tablist" aria-label={t("业务域内容")}>
              {TABS.map((item, index) => <button key={item.value} type="button" className="av-tab" role="tab"
                id={`domain-${domainId}-tab-${item.value}`} aria-controls={`domain-${domainId}-panel`} aria-selected={tab === item.value}
                tabIndex={tab === item.value ? 0 : -1} onClick={() => setTab(item.value)} onKeyDown={(event) => changeTab(event, index)}>
                {item.label}<span className="av-count">{counts[item.value]}</span>
              </button>)}
            </div>
          </header>
          <div className="av-domain-detail__body" role="tabpanel" id={`domain-${domainId}-panel`} aria-labelledby={`domain-${domainId}-tab-${tab}`} tabIndex={0}>
            <div className="av-domain-detail__toolbar">
              <label className="av-domain-detail__search"><span className="av-label">{t("搜索")}{TABS.find((item) => item.value === tab)?.label}</span>
                <input type="search" className="av-input" value={searches[tab]} onChange={(event) => setSearches({ ...searches, [tab]: event.target.value })} />
              </label>
              {tab === "members" && isOwner ? <button type="button" className="av-btn av-btn--primary" onClick={() => openMember({ user_id: "", role: "viewer" })}><Icon name="plus" />{t("添加成员")}</button> : null}
            </div>
            {tab === "metrics" ? <QueryState query={metrics} isEmpty={() => filteredMetrics.length === 0} emptyText={query ? t("没有匹配的指标") : t("此业务域暂无指标")} emptyAction={query ? clearSearch : undefined} compact>
              {() => <div className="av-table-wrap"><table className="av-table av-domain-detail__table"><thead><tr><th>{t("指标")}</th><th>{t("状态")}</th><th>{t("负责人")}</th></tr></thead><tbody>
                {filteredMetrics.map((m) => <tr key={m.id}><td><Link to={`/catalog/metrics/${m.id}`}>{m.name}</Link><span className="av-domain-detail__identifier av-mono">{m.code}</span></td><td>{m.versions[0] ? <Status value={m.versions[0].status} /> : t("暂无版本")}</td><td>{m.owner.display_name}</td></tr>)}
              </tbody></table></div>}
            </QueryState> : null}
            {tab === "assets" ? <QueryState query={assets} isEmpty={() => filteredAssets.length === 0} emptyText={query ? t("没有匹配的资产") : t("此业务域暂无资产")} emptyAction={query ? clearSearch : undefined} compact>
              {() => <><div className="av-table-wrap"><table className="av-table av-domain-detail__table"><thead><tr><th>{t("资产")}</th><th>{t("数据源")}</th></tr></thead><tbody>
                {filteredAssets.map((a) => <tr key={a.id}><td className="av-mono"><Link to={`/catalog/assets/${a.id}`}>{a.qualified}</Link></td><td>{a.datasource.name}</td></tr>)}
              </tbody></table></div>
              <Pager offset={assetOffset} shown={filteredAssets.length} page={ASSET_PAGE} hasNext={moreAssets} onChange={setAssetOffset} /></>}
            </QueryState> : null}
            {tab === "members" ? <QueryState query={domains} isEmpty={() => filteredMembers.length === 0} emptyText={query ? t("没有匹配的成员") : t("此业务域暂无成员")}
              emptyAction={query ? clearSearch : isOwner ? <button type="button" className="av-btn av-btn--primary" onClick={() => openMember({ user_id: "", role: "viewer" })}>{t("添加成员")}</button> : undefined} compact>
              {() => <div className="av-table-wrap"><table className="av-table av-domain-detail__table"><thead><tr><th>{t("成员")}</th><th>{t("账号")}</th><th>{t("权限")}</th><th className="av-domain-detail__actions">{t("操作")}</th></tr></thead><tbody>
                {filteredMembers.map((m) => <tr key={m.user_id}><td><span className="av-crumb"><span className="av-avatar">{m.display_name.slice(0, 1)}</span><b>{m.display_name}</b></span></td><td className="av-mono">{m.username}</td><td><span className="av-tag">{ROLE[m.role] ?? m.role}</span></td>
                  <td className="av-domain-detail__actions">{isOwner && m.user_id !== d.owner_id && m.role !== "owner" ? <button type="button" className="av-btn av-btn--secondary av-btn--sm" onClick={() => openMember({ user_id: m.user_id, role: m.role, name: m.display_name })}>{t("编辑权限")}</button> : <span className="av-domain-detail__muted">—</span>}</td></tr>)}
              </tbody></table></div>}
            </QueryState> : null}
          </div>
          {member ? <Drawer title={member.name ? t("编辑成员权限") : t("添加成员")} onClose={closeMember} footer={<>
            <button type="button" className="av-btn av-btn--secondary" disabled={saveMember.isPending} onClick={closeMember}>{t("取消")}</button>
            <button type="submit" form={`domain-${domainId}-member-form`} className="av-btn av-btn--primary"
              disabled={!member.user_id || !["viewer", "maintainer"].includes(member.role) || saveMember.isPending || (!member.name && !users.isSuccess)}>{saveMember.isPending ? t("保存中…") : t("保存")}</button>
          </>}>
            <form id={`domain-${domainId}-member-form`} className="av-domain-member-form" aria-busy={saveMember.isPending} onSubmit={(event) => {
              event.preventDefault();
              if (saveMember.isPending || !member.user_id || !isOwner || !["viewer", "maintainer"].includes(member.role) || (!member.name && !users.isSuccess)) return;
              saveMember.mutate({ user_id: member.user_id, role: member.role }, {
                onSuccess: () => { setMessage({ ok: true, text: member.name ? t("已更新成员权限") : t("已添加成员") }); setMember(null); },
                onError: (error) => setMemberMessage({ ok: false, text: errorText(error) }),
              });
            }}>
              {member.name ? <div className="av-field"><span className="av-label">{t("成员")}</span><strong>{member.name}</strong></div> : <QueryState query={users} isEmpty={(items) => !items.some((user) => !d.members.some((m) => m.user_id === user.id))} emptyText={t("暂无可添加的用户")} compact>
                {(items) => <label className="av-field"><span className="av-label">{t("用户")}</span><Select value={member.user_id} label={t("用户")} disabled={saveMember.isPending} onChange={(value) => setMember({ ...member, user_id: value })}>
                  <option value="">{t("选择用户")}</option>{items.filter((user) => !d.members.some((m) => m.user_id === user.id)).map((user) => <option key={user.id} value={user.id}>{user.display_name}（{user.username}）</option>)}
                </Select></label>}
              </QueryState>}
              <label className="av-field"><span className="av-label">{t("权限")}</span><Select value={member.role} label={t("成员权限")} disabled={saveMember.isPending} onChange={(value) => setMember({ ...member, role: value })}>
                <option value="viewer">{t("可查询")}</option><option value="maintainer">{t("维护者")}</option>
              </Select></label>
              <Banner message={memberMessage} />
            </form>
          </Drawer> : null}
        </div>
      ) : <Empty text={t("业务域不存在")} compact />}
    </QueryState>
  );
}

/** 业务域的示例问题（ADR 0010）：问数页、分析页显示可见业务域的示例；没有配置时由系统按口径生成。每行一个问题。 */
function ExampleQuestionsEditor({ domainId, questions, onClose }: { domainId: string; questions: { text: string; mode?: string }[]; onClose: () => void }) {
  const lines = (mode: string) => questions.filter((q) => (q.mode ?? "both") === mode).map((q) => q.text).join("\n");
  const [ask, setAsk] = useState(lines("ask"));
  const [analysis, setAnalysis] = useState(lines("analysis"));
  const [both, setBoth] = useState(lines("both"));
  const [message, setMessage] = useMessage();
  const split = (text: string, mode: string) => text.split("\n").map((line) => line.trim()).filter(Boolean).map((line) => ({ text: line.slice(0, 200), mode }));
  const body = { questions: [...split(ask, "ask"), ...split(analysis, "analysis"), ...split(both, "both")] };
  const save = useAction(() => unwrap(api.PUT("/api/domains/{domain_id}/example-questions", { params: { path: { domain_id: domainId } }, body })),
    ["domains", "example-questions"]);
  return <Drawer title={t("示例问题")} width={560} onClose={onClose} footer={<>
    <button type="button" className="av-btn av-btn--secondary" onClick={onClose}>{t("取消")}</button>
    <button type="button" className="av-btn av-btn--primary" disabled={save.isPending || body.questions.length > 12}
      onClick={() => save.mutate(undefined, { onSuccess: onClose, onError: (e) => setMessage({ ok: false, text: errorText(e) }) })}>
      {save.isPending ? t("保存中…") : t("保存")}</button>
  </>}>
    <div className="av-metric-form">
      <span className="av-help">{t("每行一个问题，最多 12 个。没有配置时，问数页和分析页按本域口径自动生成示例。")}</span>
      <label className="av-field"><span className="av-label">{t("问数示例")}</span>
        <textarea className="av-textarea" rows={4} value={ask} onChange={(e) => setAsk(e.target.value)} /></label>
      <label className="av-field"><span className="av-label">{t("分析示例")}</span>
        <textarea className="av-textarea" rows={4} value={analysis} onChange={(e) => setAnalysis(e.target.value)} /></label>
      <label className="av-field"><span className="av-label">{t("两处都显示")}</span>
        <textarea className="av-textarea" rows={2} value={both} onChange={(e) => setBoth(e.target.value)} /></label>
      {body.questions.length > 12 ? <span className="av-help av-help--danger">{t("超过 12 个问题")}</span> : null}
      <Banner message={message} />
    </div>
  </Drawer>;
}

export function NewDomain({ onClose, onCreated }: { onClose: () => void; onCreated: (id: string) => void }) {
  const users = useUsers();
  const [form, setForm] = useState({ code: "", name: "", owner_id: "", description: "" });
  const [message, setMessage] = useMessage();
  const create = useAction(() => unwrap(api.POST("/api/domains", { body: {
    code: form.code.trim(), name: form.name.trim(), owner_id: form.owner_id, description: form.description.trim() || null,
  } })), ["domains"]);
  const canSubmit = !!form.code.trim() && !!form.name.trim() && users.isSuccess && !!users.data?.some((user) => user.id === form.owner_id) && !create.isPending;
  const close = () => { if (!create.isPending) onClose(); };
  return (
    <Drawer title={t("新建业务域")} onClose={close} footer={<>
      <button type="button" className="av-btn av-btn--secondary" disabled={create.isPending} onClick={close}>{t("取消")}</button>
      <button type="submit" form="new-domain-form" className="av-btn av-btn--primary" disabled={!canSubmit}>{create.isPending ? t("创建中…") : t("创建")}</button>
    </>}>
      <form id="new-domain-form" className="av-domain-member-form" aria-busy={create.isPending} onSubmit={(event) => {
        event.preventDefault();
        if (!canSubmit) return;
        create.mutate(undefined, { onSuccess: (domain) => onCreated(domain.id), onError: (error) => setMessage({ ok: false, text: errorText(error) }) });
      }}>
        <label className="av-field"><span className="av-label">{t("名称")}</span><input className="av-input" required disabled={create.isPending} value={form.name} onChange={(event) => setForm({ ...form, name: event.target.value })} /></label>
        <label className="av-field"><span className="av-label">{t("编码")}</span><input className="av-input av-mono" required disabled={create.isPending} value={form.code} onChange={(event) => setForm({ ...form, code: event.target.value })} /></label>
        <QueryState query={users} isEmpty={(items) => items.length === 0} emptyText={t("暂无可选负责人")} compact>
          {(items) => <label className="av-field"><span className="av-label">{t("负责人")}</span><Select value={form.owner_id} label={t("负责人")} disabled={create.isPending} onChange={(value) => setForm({ ...form, owner_id: value })}>
            <option value="">{t("选择负责人")}</option>{items.map((user) => <option key={user.id} value={user.id}>{user.display_name}（{user.username}）</option>)}
          </Select></label>}
        </QueryState>
        <label className="av-field"><span className="av-label">{t("说明")}</span><input className="av-input" disabled={create.isPending} value={form.description} onChange={(event) => setForm({ ...form, description: event.target.value })} /></label>
        <Banner message={message} />
      </form>
    </Drawer>
  );
}
