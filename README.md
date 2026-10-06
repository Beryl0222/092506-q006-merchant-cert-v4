# 银发童趣商户认证档案服务

面向“银发金街与童趣乐园”特色示范街申报的服务端：把商户提交的**无障碍、价格公示、
安全服务**三类分项材料、评审意见、有效期、抽查与整改记录归档到本地 SQLite，
按街区规则实时计算对外认证状态，支撑统一标识管理、申报评审与争议复核。

## 能力

- **分项材料版本**：三类材料分别多版本上传，内容哈希去重；新版本通过后旧版本自动
  置为“被替代”，历史版本与评审意见全程保留。
- **评审意见**：评审逐版本通过/驳回并留评论；每个待审版本恰好生成一条评审待办。
- **有效期与续证**：版本带 `valid_until`，到期前 30 天进入续证窗口；
  `scan_expirations` 全街区批量扫描补续期待办，扫描任意次数幂等。
- **抽查与整改复核**：抽查不合格生成整改任务与待办，状态立即变为“整改中”；
  复核时必须凭**当前有效期内的合规版本**才能通过恢复，旧版本/过期版本不能洗白。
- **暂停/恢复**：运营方可暂停认证，对外查询**立即**变为“认证暂停”、不可使用统一
  标识；暂停只追加记录、不删除凭证，恢复后状态按规则重算（整改未结仍不可用）。
- **争议复核**：商户可对抽查结果提争议；争议成立则抽查作废、整改撤销，状态交回
  规则推导；同一抽查单重复提争议不产生第二条待办。
- **待办幂等**：所有待办以业务键唯一约束（评审按版本、续证按版本、整改按抽查单、
  争议按争议单）。服务启动自动对账：缺的补回、多的作废，重启不产生重复待办。
- **证据链**：档案事件按商户哈希串联（SHA-256），`evidence_chain` 给出某次公开
  状态对应的完整证据（生效版本、评审、抽查、整改、暂停）并校验链完整性。

## 街区状态规则

`domain.derive_status` 是唯一的状态推导入口（纯函数），优先级：

1. `suspended` 存在生效中的暂停记录；
2. `rectifying` 存在未结整改任务（含被争议但尚未成立撤销的抽查）；
3. `certified` 三类分项都有有效期内的通过版本（到期前 30 天附 `expiring_soon`
   提示，但仍可用标识）；
4. `expired` 曾认证但有效版本断档；
5. `pending` 存在待评审版本；
6. `incomplete` / `draft` 材料缺失或被驳回。

`mark_allowed=True` 仅当状态为 `certified`，过期、整改、暂停一律不得使用统一标识。

## 角色可见范围

| 角色 | 可见内容 |
| --- | --- |
| `public` 公众 | 脱敏公开状态与各分项是否合规、证据链结论（不含内部评论） |
| `owner` 商户 | 本商户完整档案、待办、争议、证据原件引用 |
| `reviewer` 评审 | 全部商户的材料版本、评审意见、争议；待办只见评审/争议两类 |
| `inspector` 抽查 | 分项状态、抽查与整改；不可见证据原件引用与评审评论 |
| `operator` 运营方 | 全量档案、暂停/恢复、全部待办 |

## 目录

- `src/merchant_cert/domain.py`：领域对象、街区状态规则与哈希工具。
- `src/merchant_cert/store.py`：SQLite 表结构、唯一约束与记录读写。
- `src/merchant_cert/service.py`：应用服务（鉴权、待办、事件、证据链）。
- `src/merchant_cert/api.py`：JSON 动作分发，`actor` 携带 `{id, role}`。
- `tests/`：基线与验收测试。

## 运行

```
PYTHONPATH=src python3 -m unittest discover -s tests
python3 -m compileall src
```

仅依赖 Python 标准库。主要请求动作：`register`、`upload_material`、
`review_version`、`record_inspection`、`resolve_remediation`、`suspend`、
`resume`、`raise_dispute`、`resolve_dispute`、`scan_expirations`、
`public_status`、`dossier`、`evidence_chain`、`list_todos`、`reconcile`。
