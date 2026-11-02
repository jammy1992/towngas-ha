# 更新日志

本仓库为 [`palafin02back/hztowngas`](https://github.com/palafin02back/hztowngas) 的通用化衍生版本。
版本号采用 2.x 是为了与上游 1.x 区分：**同样装进 `config/custom_components/hztowngas/`，会覆盖上游同名集成**。

## 2.0.0

### 新增

- **通用分公司支持**：内置 176 家港华 / 港能 / 佛燃系分公司（`branches.json`，来自港华官网 `orglist` 接口），添加集成时下拉选择，clientId 自动按「前缀 + orgCode（去下划线）」生成
- **手动 clientId 兜底**：不按该规律生成的分公司（如济南）可在同一表单手填 clientId
- **clientId 预校验**：选完分公司先请求一次授权地址，拉不起微信当场报错，不显示无效二维码
- **账户余额实体**（CNY，`savingSum`）
- **昨日用气量实体**（m³，跨两次抄表求差，状态持久化）
- **昨日气费实体**（CNY，用量 × 单价）
- **默认气价选项**：接口未返回 `gasFeeList[0].price` 时使用的兜底单价
- **条目迁移 v1 → v2**：旧条目保留令牌，无需重新扫码
- 中英双语翻译（`translations/zh-Hans.json`、`en.json`）

### 变更

- 配置流程改为「选择分公司 → 微信扫码 + 填户号」
- 重新授权时可重新选择分公司（已知 clientId 时直接出二维码）
- authCode 输入框支持直接粘贴整条跳转 URL
- 设备型号显示分公司名称
- `api.py` 的 clientId 由构造参数传入，不再读全局常量

### 修复

- 上游写死 `pe92a8wechatYH0105`（杭州余杭）导致其他分公司无法使用的问题
- `README` 中补全户号获取方式（HAR / F12）与 clientId 规则说明

---

## 上游历史（1.x，供参考）

- **1.0.7** 衍生版：把 `const.py` 的 `DEFAULT_CLIENT_ID` 改为使用者所在分公司（`pe92a8wechat` + 该分公司 orgCode）
- **1.1.0** 衍生版：新增账户余额、昨日用气量、昨日气费三个实体与 `daily.py`
- **1.0.6** 上游：微信 OAuth 扫码 + refresh_token 保活 + 6 小时取数 + HA 原生 reauth
