# 中转站指纹探针 v2（llm-relay-verifier）

> **LLM relay / API reseller fingerprint & billing verifier** — check whether a relay actually serves the model it sells: multi-dimensional model fingerprinting, randomized anti-memorization quizzes, and token-billing reconciliation, for OpenAI- and Anthropic-compatible endpoints.

给"换一家老实中转"用的验货工具：候选中转先跑探针，结论 OK 再充钱。

快速开始：

```bash
export TRANSIT_KEY="候选中转的 key"   # key 只走环境变量，绝不写盘
python3 probe.py --base-url https://候选中转域名 --model 高档模型名 --model 低档模型名
python3 probe.py --dry-run          # 先不发请求，mock 跑一遍看报告长什么样
```
v1 只有身份自报+单道谜题+30 个哈，太容易被中转商针对性绕过；v2 改为**多维指纹 + 程序化随机题库 + 计费对账**，背题和话术都没用。

v1 保留在 `probe_v1.py` / `README_v1.md`。

## 用法（本地跑，key 只走环境变量，绝不写盘/进日志）

```bash
export TRANSIT_KEY="候选中转的 key"   # 一次性，用完 unset
python3 probe.py --base-url https://候选中转域名 \
  --model 高档模型名 --model 中档模型名 --model 低档模型名 \
  --expect-vendor openai            # 按售卖宣称填: openai/anthropic/google, 不确定可省
# Anthropic 兼容站: --format anthropic（claude 系模型可用 --format auto 自动选）
python3 probe.py --dry-run          # 不发请求, mock 跑通全流程看报告长什么样
python3 -m unittest discover -s tests -v   # 自测
```

输出：`report_<时间>.md`（结论先行完整报告）+ `report_<时间>.txt`（纯文本摘要）+ `probe_result_<时间>.json`（明细，均不含 key）。
每模型约 20 次请求（`--quick` 约 12 次），成本几分钱级。

## v2 探针清单

| 探针 | 查什么 | 怎么防绕 |
|---|---|---|
| 身份多维 | 身份自报（中英双问）+ 知识截止 + 拒答风格 | 冒充别家/否认自身 = FAIL；拒答文本同时作指纹 |
| 计费对账 | 本地 token 估算（有 tiktoken 用 cl100k，无则内置启发式）vs API usage，**prompt/completion 分开**，2 组探针 | 报数超估算高界 1.8 倍 = FAIL 虚标（前科：30 个 hello 记 275 tokens） |
| tokenizer 指纹 | 5 条固定校准串（中/英/emoji/长单词/哈×30）的 prompt_tokens 向量 | 不同 tokenizer 向量必然不同；跨档向量全同 = 强套牌证据 |
| 参数透传 | temperature=0 两次复现相似度、max_tokens=5 截断、stop 序列生效 | 中转吞参数/换模型都会露馅 |
| 流式特征 | SSE chunk 数、首 token 延迟、首块占比 | 整包返回伪装流式 = WARN 假流式 |
| 随机题库 | 15 类题（算术/数列/最大公约数/素数选择/字母计数/反转/排序/JSON 格式/布尔逻辑/应用题/三段论/单位换算等），**每次随机参数+随机抽 8 题**，答案程序校验 | 题面随种子变，背下仓库原题也没用 |
| 随机序列指纹 | 借鉴 lm.ikale.io 的 Fingerpoint 思路：多语言提示让模型生成 150 个 1–355 随机整数，统计均值/方差/重复率/卡方/小号偏好 | LLM 的"随机"分布各家有特征偏倚，换模型即变 |
| 跨档套牌判定 | tokenizer 向量 + 身份文本 + 拒答文本 + 序列指纹 + 服务端 model 回显，五维相似度合成嫌疑分 | ≥50 分 FAIL，30–49 WARN，附分项证据链 |

## 判读

- 每模型出**嫌疑总分 0–100**：单项 FAIL +25、WARN +8、跨档嫌疑分折半计入。
- <30：可以小额试充，别大额。30–59：只充最低档试水。≥60：不要充。
- 任一计费/题库 FAIL 本身就是硬伤，不看总分也该谨慎。

## 局限（先说清）

- 本地 token 估算与各厂商真实 tokenizer 有偏差（Claude/Gemini 对中文尤其），阈值已放宽：只有**大幅**虚标判 FAIL，临界 WARN 请人工复核；环境装了 tiktoken 时精度更高。
- 指纹相似是嫌疑证据不是实锤：**同厂商同系列**模型共享 tokenizer/风格属正常；跨厂商、跨大版本仍高度一致才算强证据，报告里有注明。
- 随机序列指纹单次采样有波动，多维证据链一起看，别拿单项定罪。
- 只测"像不像宣称的模型"，不测中转的稳定性/速度/并发，那是充小额后实测的事。
