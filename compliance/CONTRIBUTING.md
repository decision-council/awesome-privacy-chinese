# 贡献指南

欢迎补充法规、标准、执法案例与实务解读。本库的唯一卖点是**可核验**,所以贡献规则严格但机械——照做即可通过 CI。

## 一条铁律

> **没有官方来源的内容不进库。** 每一条事实必须能落到 `sources/` 里一份归档原文上,并给出可复现的取回记录(URL + 取回时间 + sha256)。

推论:
- 不接受"据了解""业内普遍认为""某公众号称"。
- 不接受无出处的数字、日期、门槛。
- 二手报道可以引用,但**关键事实必须回到一手官方原文**;二手源在 `sources.json` 里标 `"official": false`。

## 快速开始

```bash
git clone <repo>
cd privacy-compliance-materials

python3 scripts/fetch_sources.py     # 归档所有一手来源(纯标准库,无需装依赖)
python3 scripts/validate.py          # 本地跑一遍 CI 用的校验
```

`fetch_sources.py` 只下缺失的;`--force` 全量重下,`--only <id>` 重下单个。
装了 poppler(`pdftotext`)会额外抽取 PDF 正文,没装也能跑。

## 加一份新来源

1. 在 `scripts/sources.json` 的 `sources` 数组里加一条:

```json
{
  "id": "kebab-case-slug",
  "title": "官方文件标题(逐字照抄)",
  "url": "https://www.cac.gov.cn/...",
  "category": "law | regulation | rule | standard | guide | enforcement | report | explainer",
  "publisher": "发文机关",
  "official": true
}
```

2. 跑 `python3 scripts/fetch_sources.py --only kebab-case-slug`
3. **核对 `sources/MANIFEST.json` 里的 `served_title`**——很多政府站点会对错误路径返回 200 + 门户首页。`served_title` 和你写的 `title` 对不上,就是抓错页了,换 URL 重来。
4. 把 `sources/raw/` 与 `sources/text/` 的产物一并提交(归档是本库的核心资产)。

## 加一条数据记录

`data/*.json` 与 `data/*.csv` 是机读层,字段规则见 `data/schema/`。硬性要求:

| 要求 | 说明 |
|---|---|
| `source_id` | 必须命中 `sources/MANIFEST.json` 中 `ok=true` 的一条 |
| 日期格式 | 一律 `YYYY-MM-DD`;不确定就留空,不许猜 |
| `effective_date >= promulgated_date` | 施行日不得早于发布日,校验器会拦 |
| `verification` | `confirmed`(有归档原文佐证)/ `disputed`(多源冲突)/ `unverified` |
| `disputed` 必须写 `conflict_note` | 说明谁和谁不一致、分别什么口径 |

**口径冲突不许私自选一个。** 并列写出,标 `disputed`,让读者自己判断。本库出现过的真实例子:2025 年 App 通报数量,官方口径「1100 余款」与行业梳理口径「近 4000 款」不一致,原因是是否合并各部委/地方/SDK 统计——两个都留,标明来源与口径。

## 时间口径

**一律北京时间(Asia/Shanghai, UTC+8)。** 脚本已固定该时区,不要改成 UTC 或本地时区。

## 文档写作

- `docs/` 用中文;**代码、注释、commit message 用英文**。
- 每个事实句后跟来源链接,或在文末「来源」区统一列出。
- 不确定的加 ⚠️ 并说明待核什么,不要为了完整而编。
- 法条引用给「文件名 + 条款号」,不要只写"相关规定"。

## 提交前自查

```bash
python3 scripts/validate.py
```

校验器会检查:schema 合规、日期格式与先后顺序、`source_id` 是否有对应归档、`disputed` 是否附冲突说明、id 是否重复、归档文件 sha256 是否与 manifest 一致(防篡改)。**全绿才提 PR**,CI 会再跑一遍。

## 不接受的内容

- 无来源的"整理"内容、AI 生成未经核对的摘要
- 法律意见 / 个案咨询(本库是资料库,不是律所)
- 未脱敏的个人信息、企业敏感数据——**这是个隐私合规库,自己先合规**
- 付费墙内容的全文转载
