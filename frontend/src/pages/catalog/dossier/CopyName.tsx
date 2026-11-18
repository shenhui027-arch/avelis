// 表名后的复制图标：复制完整表名（库名.表名），结果通过页面的提示条反馈。
import { Icon } from "../../../components/ui";
import { t } from "../../../i18n";

export function CopyName({ qualified, onMessage }: { qualified: string; onMessage: (m: { ok: boolean; text: string }) => void }) {
  const copy = async () => {
    // 没有剪贴板接口、或浏览器拒绝写入（非安全上下文、权限被拒）时给同一句可操作的提示，不显示浏览器的原始报错
    try {
      await navigator.clipboard.writeText(qualified);
      onMessage({ ok: true, text: t("已复制表名 {{name}}", { name: qualified }) });
    } catch { onMessage({ ok: false, text: t("当前浏览器不支持复制，请选择表名后复制") }); }
  };
  return (
    <button type="button" className="av-btn av-btn--ghost av-btn--sm av-btn--icon av-dossier-copy" onClick={copy}
      aria-label={t("复制表名")} data-tip={t("复制表名")}><Icon name="copy" small /></button>
  );
}
