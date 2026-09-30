# AI Paper Weekly

AI 前沿论文自动检索、中文技术解读生成、微信公众号文章输出系统。

**当前状态：Phase 1（本地 MVP）已完成。** 在 Mac 上运行一条命令，即可产出一篇包含 3～4 篇真实论文的中文解读，输出 Markdown 和可直接粘贴到公众号的 HTML。论文来自 **arXiv / 期刊 / 会议论文集三条通道**，优先选 2026 年正式发表或录用的顶刊顶会论文。

---

## 一、它做什么

```
按星期选主题组（周一/周三/周五），并按周计划算出本期目标篇数
      ↓
三条通道同时检索 2026 年论文：
  arXiv API（最近 14 天，不足回退 30 天）
  OpenAlex 期刊通道（近 180 天的正式出版记录）
  会议论文集（ICLR virtual / CVF / ACL Anthology / PMLR / NeurIPS）
      ↓
按研究方向打关键词分 + 去重（arXiv ID / DOI / 官方论文页地址 + 跨来源标题归并）
      ↓
核验发表状态（期刊查 Crossref，会议以官方论文集为准，arXiv 查 OpenAlex）
      ↓
大模型逐篇生成结构化中文解读
      ↓
规则校验：链接可追溯性、数字一致性、发表状态措辞
      ↓
输出 output/*.md、output/*.html、output/*.validation.md
      ↓
记录到 data/published.json（供下次去重，并据此统计本周已完成篇数）
```

一条贯穿全项目的原则：**宁可少写，也不编造。** 校验器会拦下"原文里找不到的链接"和"原文里对不上的数字"；每周凑不满 10 篇时，宁可少发，也不用预印本凑数。

---

## 二、目录结构

```
AI论文推送/
├── main.py                          # 入口：CLI 编排
├── models.py                        # Paper / PaperAnalysis / ArticleRecord
├── utils.py                         # 原子写文件、文件名安全化
├── requirements.txt
├── .env.example                     # 环境变量模板（复制成 .env）
├── config/
│   └── topics.yaml                  # 主题、关键词、白名单、检索参数（改这里不用改代码）
├── collectors/
│   ├── arxiv_client.py              # arXiv 检索（限流退避 + 重试）
│   ├── openalex_client.py           # OpenAlex：期刊检索 + 补 DOI / 发表来源
│   ├── journal_client.py            # 期刊通道：按 OpenAlex source 找近期正式发表
│   ├── crossref_client.py           # Crossref：期刊论文的权威出版证据
│   ├── publication_verifier.py      # 发表状态核验（纯规则，不用大模型）
│   └── venues/                      # 会议论文集通道
│       ├── base.py                  #   采集基类 + 种子数据缓存（data/cache）
│       ├── iclr_virtual.py          #   ICLR 官方 virtual 站点
│       ├── cvf.py                   #   CVPR / ICCV / ECCV（CVF Open Access）
│       ├── acl_anthology.py         #   ACL / EMNLP / NAACL（全量 BibTeX）
│       ├── pmlr.py                  #   ICML / COLM（PMLR）
│       └── neurips.py               #   NeurIPS proceedings
├── services/
│   ├── paper_filter.py              # 打分、去重、跨来源归并、选文
│   ├── weekly_plan.py               # 周计数、缺额结转、周内去重
│   ├── paper_reader.py              # 组装证据快照 + 大模型输入
│   ├── prompts.py                   # Prompt 模板（改风格改这里）
│   ├── llm_client.py                # OpenAI 兼容协议客户端
│   ├── article_generator.py         # 解读、导读、拼装 Markdown
│   └── article_validator.py         # 事实校验
├── publishers/
│   ├── markdown_publisher.py
│   ├── html_publisher.py            # 内联样式，兼容公众号
│   ├── notifier.py                  # 出稿/失败通知推到微信（PushPlus）
│   └── wechat_mp.py                 # 公众号草稿箱（access_token / 封面素材 / draft）
├── storage/
│   └── paper_repository.py          # published.json 原子读写
├── templates/
│   └── article.html
├── .github/
│   └── workflows/weekly.yml         # 云端定时：周一三五 09:00（北京）自动跑
├── data/
│   ├── published.json               # 历史库（去重依据，云端跑必须入库）
│   └── cache/                       # 论文集大文件缓存（40MB BibTeX 等，TTL 7 天）
├── output/                          # 每期产物
└── tests/                           # 162 个单元测试，不联网
```

---

## 三、安装

```bash
cd ~/Desktop/AI论文推送

# 1) Python 3.12（已装可跳过）
brew install python@3.12

# 2) 虚拟环境 + 依赖
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt

# 3) 配置密钥
cp .env.example .env
# 然后编辑 .env，填入 LLM_API_KEY
```

### `.env` 关键项

| 变量 | 说明 |
|---|---|
| `LLM_API_KEY` | **必填**。阿里云 MaaS token-plan 控制台创建，密钥格式为 `sk-sp-` 开头 |
| `LLM_BASE_URL` | 默认 `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` |
| `LLM_MODEL` | 默认 `deepseek-v4.1-flash` |
| `LLM_MAX_TOKENS` | 输出上限，默认 32768。**推理模型必看**，见下方说明 |
| `LLM_TIMEOUT_SECONDS` | 默认 300。推理模型较慢，给足时间 |
| `OPENALEX_MAILTO` | 选填。填邮箱后进入 OpenAlex 礼貌池，配额更稳 |
| `CROSSREF_MAILTO` | 选填。填邮箱后进入 Crossref 礼貌池；不填时复用 `OPENALEX_MAILTO` |
| `PUSHPLUS_TOKEN` | 选填但推荐。填了才会把出稿/失败消息推到微信，见第九节 |
| `WECHAT_APPID` / `WECHAT_APPSECRET` | 选填。配 `publishing.wechat.enabled: true` 后用于自动建公众号草稿 |
| `WECHAT_COVER_IMAGE` | 选填。封面图本地路径，建草稿时必填（微信要求图文消息带封面） |

**密钥必须和 base_url 同源。** 同一个阿里云账号下，不同产品的密钥格式不一样，混用会报 `invalid_api_key`：

| 平台 | base_url | 密钥格式 |
|---|---|---|
| MaaS token-plan（本项目在用） | `https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1` | `sk-sp-` 开头，约 115 字符 |
| 百炼 DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `sk-` + 32 位十六进制，共 35 字符 |

> **关于 ChatGPT Plus**：它只覆盖网页版使用，**不含 API 调用额度**。要用 OpenAI 的 API 需另在 platform.openai.com 充值。

### 推理模型与 max_tokens（重要）

`deepseek-v4.1-flash` 是**推理模型**，它的思考过程（reasoning tokens）**同样计入 `max_tokens` 额度**。所以：

- 把 `max_tokens` 设得太小，会先被思考过程吃光，结果是**返回空内容**或 `finish_reason=length` 截断；
- 项目默认给到 32768，足够覆盖「思考 + 约 2500 token 的结构化 JSON 输出」；
- 代码会检测 `finish_reason == "length"`，直接报出当前用量和调整建议，而不是原样重试（原样重试只会再次截断，白烧额度）。

顺带澄清一个常见误解：**「上下文窗口」不是可配置参数**，它由模型本身决定；请求里能控制的只有输出的 `max_tokens`。本项目按「摘要 + 元数据」取值，单次输入仅 2～4k token，离任何模型的窗口上限都很远。

---

## 四、运行

```bash
# 0) 自检：配置 / arXiv / OpenAlex / 密钥 / 历史库
.venv/bin/python main.py --selftest

# 1) 先干跑，只检索和筛选，不花大模型额度
.venv/bin/python main.py --dry-run

# 2) 正式生成一期
.venv/bin/python main.py

# 3) 查看历史
.venv/bin/python main.py --history
```

其他参数：

| 参数 | 用途 |
|---|---|
| `--group g_mon\|g_wed\|g_fri` | 手动指定主题组 |
| `--date 2026-09-21` | 把某天当作运行日（决定主题组与文件名） |
| `--force` | 忽略"本期已生成"，强制重跑 |
| `--dry-run` | 只检索与筛选，并打印候选池前 10 名 |

**幂等性**：同一运行日 + 同一主题组只会生成一期。重复运行会直接跳过，不会重复消耗额度，也不会重复推荐论文。

### 输出物

```
output/2026-09-21-vol01-g_mon.md              ← 主文章（Markdown）
output/2026-09-21-vol01-g_mon.html            ← 手机阅读 + 可直接粘贴公众号
output/2026-09-21-vol01-g_mon.validation.md   ← 自动校验报告，发布前必读
```

---

## 五、测试

```bash
# 当前共 162 个用例
.venv/bin/python -m pytest tests -q
```

162 个测试覆盖：去重（arXiv ID / DOI / 官方论文页地址 + 跨来源标题归并）、标题加权、分数门槛、选文多样性、发表状态核验的 8 种分支、Crossref 复核的 5 种分支、校验器对「编造链接 / 对不上的数字 / 把预印本说成顶会」的拦截，历史库的读写与幂等，期刊通道（倒排摘要还原、来源解析），周计划（周起始日、缺额结转、上限截断、按期号日期统计、周内去重键、预印本硬上限），配置键是否真的驱动行为（历史库路径、时区、输出格式、预印本开关、去重开关），微信通知与公众号草稿箱（token 不泄露、通知失败不影响出稿、微信「HTTP 200 但 errcode 非 0」的识别、字段超限本地拦截），以及 5 个会议采集器的页面解析。全部不联网。

会议采集器的测试用**真实页面片段**做 fixture，页面改版时这些测试会失败，起到告警作用。

---

## 六、调参：选文质量怎么调

选文质量几乎全部由 `config/topics.yaml` 决定，不需要改代码。

**打分公式**

```
分数 = 标题关键词得分 × 2 + 摘要关键词得分 + 新鲜度(2.0) + 发表状态加权
       强关键词 3 分 / 普通关键词 1 分      （14 天内）      正式发表、录用 1.5
                                                          待核实 0.75
                                                          预印本 0
```

| 症状 | 改哪个 |
|---|---|
| 选进来的论文跑偏 | 调高 `search.min_score`，或在 `topics` 里把该词从 `keywords` 删掉 |
| 候选太少、选不满 | 调低 `search.min_score`，或把泛词加进 `keywords` |
| 一期全是同一个方向 | 调低 `search.diversity_ratio`（如 0.6） |
| 想多推正式发表论文 | 调高 `STATUS_BONUS` 中正式发表的值，或调高 `min_score` |
| 主题安排要改 | 改 `schedule` 与 `groups` |

用一个命令看调参效果：

```bash
.venv/bin/python main.py --dry-run --group g_mon
# 会打印候选池前 10 名及各自命中的方向与分数
```

### 每周计划：周一 3 / 周三 3 / 周五 4，合计 10

`weekly_plan` 决定每期发几篇：

- 基准篇数来自 `schedule.<weekday>.select_count`（3 / 3 / 4）；
- **缺额结转**：本周前面期次没凑满的部分，自动加到后面的期次上，但单期不超过 `max_per_issue`，也不会超过「本周剩余目标」；
- **只有成功生成并落盘的文章才计入本周完成数**，所以某期跑失败了不会留下"已发一半"的脏状态；
- 周目标已达成时，本期直接跳过不生成（日志里会说明）。

运行时会打印一行规划结果，例如：

```
本周（自 2026-09-28 起）目标 10 篇，此前已完成 0 篇；本期目标 3 篇（基准 3，最低 2）
```

低于 `min_select_count` 就不生成文章——宁可少发，也不编造。

### 配置生效范围：哪些键真的驱动行为

`config/topics.yaml` 里的键分两类。改了没反应的键都在文件里标了「声明性」，下面说明原因。

| 键 | 状态 | 说明 |
|---|---|---|
| `timezone` | 生效 | 文章生成时间按它计算（CI 机器是 UTC 时尤其重要） |
| `weekly_plan` 除 `allow_under_target` | 生效 | 周计数、缺额结转、单期上限、周内与历史去重 |
| `weekly_plan.allow_under_target` | 声明性 | 程序本来就允许少出，没有「凑数」分支 |
| `schedule` / `groups` / `topics` | 生效 | 主题安排与打分关键词 |
| `search` 的年份、时间窗、门槛、多样性、`journals` | 生效 | |
| `search.verify_publication` | 生效 | `false` 时整期跳过发表状态核验 |
| `retrieval.official_venues` 的 `enabled` / `sources` / `cache_*` / `abstract_limit_per_issue` | 生效 | |
| `retrieval` 其余（通道开关、`date_field`、`abstract_strategy`、`verify_final_candidates`、`merge_duplicate_records`） | 声明性 | 程序只有一种实现，无法从这里切换 |
| `publication.allow_preprints` / `max_preprints_per_issue` | 生效 | `allow_preprints: false` 时上限直接归零 |
| `publication` 其余（`allowed_statuses`、`priority`、`require_verified_venue`、`year_basis` 等） | 声明性 | 见下方说明 |
| `venue_whitelist` | 生效 | 白名单匹配 |
| `article` 的 `output_formats` / `field_budgets` / `synthesis_budgets` / `tolerance` | 生效 | |
| `article` 的 `language` / `depth` / `sections` / `include_source_links` 等 | 声明性 | 文章结构由 prompts 与渲染器固定 |
| `publishing.save_markdown` / `save_html` | 生效 | 与 `output_formats` 共同决定输出哪些文件 |
| `publishing.notify.enabled` | 生效 | `false` 时出稿后不推微信 |
| `publishing.wechat.enabled` | 生效 | `true` + 配好三件套凭据时自动建草稿（云端受 IP 白名单限制） |
| `publishing.mode` / `wechat.auto_publish` / `auto_mass_send` / `require_manual_review` | 声明性 | 订阅号没有发布与群发权限，置 `true` 只会记一条告警 |
| `storage.paper_history_path` | 生效 | 相对路径按项目根目录解析 |
| `storage.deduplication_keys` | 声明性 | 程序用的是超集（多 OpenAlex ID、官方论文页地址），否则会议论文没有去重键 |
| `storage.track_publication_status` | 声明性 | 依赖微信发布流程 |

**为什么有些键故意不做成开关**：`require_verified_venue`、`allow_unverified_as_published`、`allow_fabricated_papers` 这几项的值是论文真实性的底线。把它们做成可关闭的开关，等于留一个「把没核验过的来源写成正式发表」的口子，与本项目的目标直接冲突。程序恒按最严格的方式执行，不接受配置放宽。

`tests/test_config_wiring.py` 专门守住这条线：每接一个键，都有一处测试能在它失效时报警。

---

## 七、论文来源：三条通道

| 通道 | 实现 | 发表状态 | 实测产出（2026-09，target_year=2026） |
|---|---|---|---|
| arXiv | `collectors/arxiv_client.py`，按分类 + 提交日期检索 | 查 OpenAlex 判断是否已被收录 | 每次约 800 篇候选，**多为预印本** |
| 期刊 | `collectors/journal_client.py`，按 OpenAlex `source` + 出版日期 | 由 **Crossref** 复核后定案 | 5 本 AI 期刊近 180 天约 100 篇 |
| 会议 | `collectors/venues/`，直接解析 5 个官方论文集站点 | 官方论文集即为发表证据 | **12154 篇**（ICLR 5468 + CVPR 4042 + ACL 2644） |

### 会议论文集通道：实测可用的 5 个来源

| 来源 | 抓取方式 | 实测结果 |
|---|---|---|
| ICLR virtual | `/virtual/2026/papers.html` + 单篇 poster 页取摘要 | ✅ 5468 篇（2026） |
| CVF Open Access | `/CVPR2026?day=all`（约 12MB）+ 单篇页 `id="abstract"` | ✅ 4042 篇（CVPR 2026） |
| ACL Anthology | 全量 `anthology+abstracts.bib.gz`（42MB，gzip 流式逐条解析） | ✅ ACL 2026 = 2644 篇，其中 2642 篇自带摘要 |
| PMLR | 首页卷列表定位卷号 + 卷页解析 | ✅ ICML 2025 = 3330 篇；**ICML 2026 卷尚未发布，程序告警并跳过** |
| NeurIPS proceedings | `/paper_files/paper/{year}` | ✅ 2024 = 4493 篇；**2025 / 2026 索引页尚未上线，程序告警并跳过** |

设计上的两个要点：

- **列表页只给标题，摘要必须逐篇再抓一次**。所以流程是「整卷索引 → 标题粗筛 → 只给候选补摘要」，单期上限由 `retrieval.official_venues.abstract_limit_per_issue`（默认 120）控制。到达上限后剩下的论文按标题参与打分——宁可少抓，也不为了打分好看去无限抓页面。
- **大文件必须缓存**。ACL 的 42MB BibTeX、CVF 的 12MB 索引都存在 `data/cache/`（TTL 7 天），同一天多次运行不会重复下载。

### 打不通的路（都实测过，不是没试）

| 路径 | 实测结果 |
|---|---|
| OpenReview API（ICLR/NeurIPS 官方评审平台） | 403 `ChallengeRequiredError`，需过人机验证 → 改走 ICLR 官方 virtual 站点 |
| OpenAlex 的会议来源 | ICML 记录最新到 **2021-07**，NeurIPS 2021-12，ICLR 2021-05，拿不到近期论文 |
| DBLP | 返回 `Making sure you're not a bot!` 拦截页 |
| Semantic Scholar | 429 限流 |

### 期刊通道的实际覆盖（实测）

| 期刊 | OpenAlex 近 180 天可用论文 |
|---|---|
| Nature Machine Intelligence | 50 篇 |
| IEEE TPAMI | 50 篇 |
| TMLR | 0 篇 |
| TACL | 0 篇 |
| JMLR | **0 篇**（OpenAlex 的记录都挂在 arXiv 名下，按来源过滤查不到） |

两个坑：

- **期刊本身在半年窗口内也只产出这百来篇**，所以期刊通道是「锦上添花」，主力仍要看会议论文集通道。
- **57% 的期刊论文在 OpenAlex 里没有摘要**（出版社不提供）。所以 `require_title_match` 只对 arXiv 论文生效——期刊标题是自然语言写法（如 "Causal evidence that language models use confidence to drive behavior"），套用术语门槛会把它们全部挡掉。

综合刊 Nature / Science 已从配置中移除：实测 95 篇候选里 60 篇来自这两家且几乎全是非 AI 内容。

## 八、发表状态怎么判定

四种状态，标签由**纯规则**给出，绝不由大模型判断：

| 状态 | 触发条件 |
|---|---|
| 正式发表 | 会议论文：来自会议官方论文集；期刊论文：Crossref 中存在该 DOI 的注册记录，且刊物命中 `venue_whitelist` |
| 正式录用 | 当前不自动判定（需要会议官网等可靠来源，留待后续） |
| 预印本 | 未检索到正式发表记录，且 arXiv 页面也没有任何待核验线索 |
| 待核实 | 其余情况，包括：来源不在白名单、arXiv 备注提到录用、核验服务不可用、超出本次核验配额 |

刻意保守的地方：

- **不认 OpenAlex 的来源名**（`openalex_venue_match_is_sufficient: false`）。OpenAlex 的来源名是聚合结果，出现「Nature Machine Intelligence」字样不等于论文真登在这本刊上，所以期刊论文一律再用 **Crossref**（DOI 注册机构，数据直接来自出版商）复核一遍。
- Crossref 查不到注册记录 → 降级为「待核实」，并在依据里写明"OpenAlex 显示发表于 X，但 Crossref 中没有该 DOI 的注册记录"。
- **核验服务本身挂了不改判**。Crossref 超时/限流时保留原结论并记日志——服务不可用不等于论文没发表，因故障把一个真实结论改错是更严重的错误。
- arXiv 页面上的 `comment`（例如 "Accepted at ICML 2027"）**不会被当作录用证据**，只会写进"待核实"的依据里。
- 白名单匹配带词边界，`ACL` 不会误匹配 `NAACL`。
- OpenAlex 挂掉时不会把所有论文写成"预印本"，而是写"待核实：OpenAlex 本次不可用"。
- **每期最多 1 篇非正式发表论文**（`max_preprints_per_issue`）。超出时按入选顺序截断，再从候选池里用正式发表/正式录用的论文补位；补不齐就少出，不用预印本凑数。

---

## 九、微信接收：通知与草稿

### 权限实况（已核实）

个人订阅号**无法完成微信认证**，「程序自动发布/群发」这条路走不通：

| 接口 | 个人订阅号 | 结论 |
|---|---|---|
| Access Token / 素材 / 上传文章图片 | 可用 | — |
| 草稿箱 `draft/add` | **可用** | 程序可自动建草稿 |
| 发布 `freepublish/submit` | 不可用 | 官方标注适用范围为「仅认证」（企业主体） |
| 群发 `message/mass/*` | 不可用 | 订阅号需认证后才可用 |

所以自动化上限是**自动建草稿**，群发必须你人工点一次。

### 路线一：通知推到微信（云端可用）

出稿成功、候选不足、运行失败三种情况都会推一条消息到微信，走 [notifier.py](file:///Users/yu/Desktop/AI论文推送/publishers/notifier.py)：

1. 到 [pushplus.plus](https://www.pushplus.plus) 扫码登录并**关注「pushplus 推送加」公众号**（消息是从这个服务号发给你的，不关注收不到）；
2. 完成**实名认证**——注意这一步是**收费**的：官网写明「实名认证是付费的，且不支持退款」，需付第三方认证服务费，或开通一个月会员可免认证费。未实名调用发送接口会返回 `905`；
3. 复制首页的「用户令牌」，填进 `.env` 的 `PUSHPLUS_TOKEN`，或配成 GitHub Actions 的 Secret；
4. `python main.py --selftest` 会显示「微信通知 —— 已配置」。

消息长这样（云端跑时最后一行是可点的全文链接）：

```
AI 论文周报 Vol.12 已生成
**长上下文推理与智能体**
- 主题组：大模型推理与 AI Agent
- 本期论文：3 篇
- 待核实项：2 条

1. [会议][正式发表] Long-Context Reasoning Through Proxy-Based CoT Tuning
2. [会议][正式发表] InftyThink: Breaking the Length Limits…
3. [会议][正式发表] OrchestrationBench: LLM-Driven Agentic Planning…
[打开全文](https://github.com/you/ai-paper/blob/main/output/2026-10-05-vol12-g_mon.md)
```

没配 token 时静默跳过，不影响出稿。**推送失败也绝不影响出稿**——第三方服务抖动不该让一期文章白跑。

### 让通知里的链接在国内点得开（Gitee 镜像）

消息末尾那个链接默认指向 GitHub。但 **github.com 在国内直连不通**，手机上点它会一直转圈，必须挂代理才打得开——这等于把最有用的一步废掉了。

配好 Gitee 镜像后，工作流会把同一个提交同步到 Gitee，通知里的链接改成 `gitee.com` 地址，手机不挂代理也能直接点开：

1. 到 [gitee.com](https://gitee.com) 注册并完成实名认证；
2. 新建一个仓库（**不要**勾选「初始化仓库」，要空的），名字随意，建议与 GitHub 同名；
3. 「设置 → 私人令牌 → 生成新令牌」，勾选 `projects` 权限，复制令牌；
4. 填进 `.env` 的 `GITEE_REPO`（形如 `用户名/仓库名`）和 `GITEE_TOKEN`；
5. 同步到仓库：`GITEE_REPO` 存为 **Variable**，`GITEE_TOKEN` 存为 **Secret**。

两项留空时，工作流会打印「跳过 Gitee 同步」并继续，不影响出稿；通知里的链接退回 GitHub。

### 路线二：自动建公众号草稿

配好 `WECHAT_APPID` / `WECHAT_APPSECRET` / `WECHAT_COVER_IMAGE`，再把 `config/topics.yaml` 的 `publishing.wechat.enabled` 改成 `true`，程序出稿后会自动建草稿，你在公众号后台确认后点群发。

两个微信侧的限制必须知道：

1. **封面图必须有**：`draft/add` 的 `thumb_media_id` 是必填，且必须是**永久素材**。程序会用 `material/add_material` 自动上传 `WECHAT_COVER_IMAGE` 指向的本地图片换取 media_id。
2. **调用来源 IP 必须在白名单里**，否则返回 `40164`。

> **重要：这条路在 GitHub Actions 上跑不通。**
> 公众号的服务端接口要求调用来源 IP 在后台的「API IP 白名单」内，白名单只支持具体 IP 或 `172.0.0.1/24` 这种小网段（上限 10 条），而 GitHub Actions 的 runner 出口 IP 来自一整个动态池，无法覆盖。
> 想自动化建草稿，需要有一个固定出口 IP 来调接口，可选：
> - 微信云托管（官方称「免鉴权调用微信开放服务接口」，无需 IP 白名单）；
> - 一台有固定 IP 的云主机 / 云函数；
> - 或在白名单内的机器上手动跑本程序。
>
> 因此**默认配置里 `publishing.wechat.enabled` 是 `false`**：云端只生成 + 通知，你收到微信消息后点开全文，再手动贴进公众号后台。

---

## 十、云端定时（GitHub Actions）

工作流见 [.github/workflows/weekly.yml](file:///Users/yu/Desktop/AI论文推送/.github/workflows/weekly.yml)：周一/三/五北京时间 09:00（cron `0 1 * * 1,3,5`，UTC）自动生成一期。

**开通步骤**（当前目录还不是 git 仓库，这几步需要你操作）：

1. 在 GitHub 建一个仓库（**建议私有**：历史库和文章都会提交进去）；
2. 本地初始化并推送：

```bash
cd /Users/yu/Desktop/AI论文推送
git init && git add . && git commit -m "init"
git remote add origin git@github.com:<你的用户名>/<仓库名>.git
git push -u origin main
```

3. 在仓库 `Settings → Secrets and variables → Actions` 里加：

| 名称 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `LLM_API_KEY` | Secret | 是 | 大模型密钥 |
| `PUSHPLUS_TOKEN` | Secret | 建议 | 微信通知 |
| `OPENALEX_MAILTO` | Secret | 否 | 进礼貌池，限流更少 |
| `CROSSREF_MAILTO` | Secret | 否 | 同上 |
| `WECHAT_APPID` / `WECHAT_APPSECRET` | Secret | 否 | 建草稿用，但见第九节的 IP 限制 |
| `GITEE_TOKEN` | Secret | 建议 | 同步到 Gitee 用的私人令牌，让手机能直接点开链接 |
| `GITEE_REPO` | Variable | 建议 | 形如 `用户名/仓库名` |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_MAX_TOKENS` | Variables | 否 | 留空则用程序默认值 |

4. 到 `Actions → AI 论文周报 → Run workflow` 手动跑一次，别等定时。

工作流做的三件事：跑一遍单元测试 → 生成一期 → 把 `data/published.json` 和 `output/` 提交回仓库。
`output/` 本地是 gitignore 的，云端用 `git add -f` 强制提交——那是为了让文章能通过仓库送到你手机上。
退出码 3（候选不足，本期未生成）被当作正常结果，不会让流水线变红，消息已经推到微信了。

`data/published.json` 必须持久化，否则每次都是全新环境、每周重复推荐同一批论文。仓库就是它的持久化位置。

---

## 十一、常见异常

| 现象 | 原因与处理 |
|---|---|
| `缺少 LLM_API_KEY` | 没建 `.env`，或没填密钥。`cp .env.example .env` 后填写 |
| `invalid_api_key` / `Incorrect API key` | 密钥格式与 base_url 不同源。对照上方表格，`sk-sp-` 开头的是 MaaS token-plan，`sk-` + 32 位十六进制的是百炼 |
| `Model not exist` | `LLM_MODEL` 在该端点不存在。用 `--selftest` 或直接调 `/models` 看可用列表 |
| `大模型返回空内容` / `输出被 max_tokens 截断` | 推理模型的思考过程吃光了额度。调大 `LLM_MAX_TOKENS`，或在 `.env` 里改成非推理模型 |
| `arXiv 检索失败，已重试 3 次` | 网络不通或被限流。arXiv 要求请求间隔 ≥3 秒，客户端已自动退避；稍后重试 |
| `OpenAlex 不可用` | 非致命。本期论文会全部标记为「待核实」，不会中断生成 |
| `无法从模型返回中解析出 JSON` | 模型没按 JSON 输出。已自动重试；持续失败可换 `LLM_MODEL` |
| `未找到 ICML 2026 的论文集卷，官方尚未发布` | PMLR / NeurIPS 的当年卷要等会议开完才上线。这是正常情况，程序只告警、不编造，本期该来源为空 |
| `会议摘要抓取已达本期上限` | 候选太多，触发了 `abstract_limit_per_issue`（默认 120）。剩余论文按标题参与打分，如需覆盖更多可调大该值（代价是每篇约 1 秒） |
| `Crossref 不可用，本期未能复核` | 期刊论文保留原结论，不因服务故障改判。稍后重跑即可 |
| `本周目标已达成，本期不生成文章` | `weekly_plan` 的周计数已满 10 篇。属正常跳过 |
| `本期只选出 N 篇，低于最低要求` | 候选不足。调低 `min_score` 或扩大到 30 天 |
| `历史库 ... 无法解析` | `data/published.json` 损坏。程序会中止以免覆盖，请手工修复或备份后删除重建 |
| 文章里出现"来源之外的链接" | 校验器拦住了疑似编造的链接。请打开原文核对后再决定是否保留 |
| 校验报告里出现数字提醒 | 默认是「文章里的数字在论文原文中找不到」。注意已抹平 `5.5x` / `5.5×` / `5.5 倍` / LaTeX `5.5$\times$` 这类等价写法，报出来的基本是真需要核对的 |
| `本期已存在，跳过` | 幂等保护。要重跑加 `--force`（注意：去重索引里已有的论文不会再次入选，重跑会换一批论文） |
| `微信通知发送失败` | 非致命，只记日志。常见原因：`PUSHPLUS_TOKEN` 填错、账号未实名认证、当日发送条数超限 |
| `公众号草稿未创建：... 40164` | 调用来源 IP 不在公众号 IP 白名单里。这是微信侧限制，不是程序问题，见第九节 |
| `未配置 WECHAT_COVER_IMAGE，无法创建草稿` | 微信要求图文消息必须带封面。填一张本地 jpg/png 的路径即可，程序会自动上传成永久素材 |
| `正文 N 字符，超过微信上限 20000 字符` | 文章太长。调小 `config/topics.yaml` 中 `article` 段的字数预算，或把 `publishing.wechat.enabled` 关掉 |
| Actions 上红了但没收到微信 | 通知本身失败不影响出稿。检查 Secret 名是否拼错（大小写敏感） |

退出码：`0` 正常（含"跳过"）、`1` 运行错误、`3` 候选/校验不足导致本期未生成。

---

## 十二、后续阶段

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 1 | 本地检索 → 解读 → 出稿 | ✅ 已完成 |
| Phase 2 | GitHub Actions 定时运行、历史库持久化、失败通知 | ✅ 已完成（[weekly.yml](file:///Users/yu/Desktop/AI论文推送/.github/workflows/weekly.yml)，需你建仓库配 Secrets） |
| Phase 3 | 自动创建公众号草稿 + 人工群发 | ⚠️ 代码已就绪（`publishing.wechat.enabled`），但受 IP 白名单限制，云端暂时跑不通，见第九节 |

Phase 2 的两个注意点：GitHub Actions 的运行环境是临时的，`data/published.json` 必须提交回仓库；cron 用 UTC 表达式，北京时间 9:00 对应 `0 1 * * 1,3,5`。

---

## 十三、成本

- arXiv、OpenAlex：免费，无需密钥。
- 大模型：一期约 4 次调用（3 篇解读 + 1 次导读总结），实测单期耗时 1～2 分钟。推理模型的思考过程也计费，成本比非推理模型高。
- 建议先用 `--dry-run` 确认选文，再正式跑。
- 想省钱可以在 `.env` 里把 `LLM_MODEL` 换成同端点下的非推理模型（如 `deepseek-v3.2`）。