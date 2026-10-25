# 中转站指纹探针 v1

给"换一家老实中转"用的验货工具:候选中转先跑探针,结论 OK 再充钱。

## 用法(本地跑,key 不进任何文件)

```bash
export TRANSIT_KEY="候选中转的 key"   # 一次性,用完 unset
python3 probe.py --base-url https://候选中转域名 \
  --model 高档模型名 --model 中档模型名 --model 低档模型名 \
  --expect-vendor openai   # 按售卖宣称填: openai/anthropic/google, 不确定可省
```

## 判读(每行一条,CSV)

- P1 身份自报: `FAIL_否认自身存在`(10-09 apiclaude 6.1sol 同款)/`WARN_未提及预期厂商`
- P2 计费: 30 个"哈"只值约 30-60 token,reported/实际 >3 倍判虚标
  (前科: apiclaude 把 30 个 hello 记成 275)
- P3 能力: 谜题答案须含 2,答错=档位能力不符
- 跨档对比: 多档 P1 回答零差异=套牌(10-14 lm.ikale.io 同款信号)

## 通过标准

三档 P1 各自认领正确身份、P2 无虚标、P3 全对,才充钱。任一 FAIL 直接 pass 这家。

明细 JSON 存 `probe_result_*.json`(不含 key),可直接贴回来分析。
