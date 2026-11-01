## 改了什么

<!-- 一两句话写清这个 PR 做了什么、为什么。关联的 issue 写 Closes #123。 -->

## 怎么验证的

<!-- 写实际运行过的命令与结果；没跑的写在“尚未验证”。 -->

- [ ] 后端单元测试：`AVELIS_SECRET=offline-unit-test-secret-0123456789 AVELIS_ENV_FILE=/nonexistent uv run --frozen python -m pytest tests/unit -q -p no:cacheprovider`
- [ ] 前端构建：`pnpm build`（frontend）
- [ ] 界面改动附截图

## 尚未验证 / 已知局限

<!-- 没有覆盖的环境、引擎或场景；没有就写“无”。 -->

## 检查

- [ ] 产品代码没有写入具体业务的表名、指标或规则（ADR 0010）
- [ ] 没有提交口令、密钥、本机路径或真实业务数据
- [ ] 行为、配置或命令变化已同步更新文档
