#!/usr/bin/env python3
"""中转站指纹探针 v1 —— 候选中转先验再充钱。

用法:
    export TRANSIT_KEY="候选中转的key"   # key 只走环境变量,脚本不写盘、不打印
    python3 probe.py --base-url https://xxx.com --model gpt-6.1-sol --expect-vendor openai
    python3 probe.py --base-url https://xxx.com --model A --model B --model C   # 跨档一致性对比
    python3 probe.py --dry-run   # 只打印探针清单,不发请求

探针(全部来自她 10-09/10-13 亲手验证过的方法):
  P1 身份自报   : 问"你是什么模型、哪家公司开发的",看是否承认/冒充/否认
  P2 计费探针   : 让模型只回 30 个"哈",对比 usage.completion_tokens 与实际字数
                 (前科: apiclaude 把 30 个 hello 记成 275 tokens)
  P3 能力谜题   : 一道有确定答案的逻辑题,横测档位真实能力
  P4 跨档对比    : 多个 --model 时,对比各档身份回答与能力是否"零差异"(套牌信号)

输出: 每行一条结论(CSV 风格),明细 JSON 存 probe_result_<时间>.json(不含 key)。
判定阈值见 VERDICTS 注释,可按经验调。
"""
import argparse, json, os, sys, time, urllib.request, urllib.error
from datetime import datetime

PROBES = {
    "P1_identity": [
        "请用一句话介绍你自己:你是什么模型,由哪家公司开发?",
        "As an AI model, who are you and who developed you? 用中文回答。",
    ],
    "P2_billing": "请只回复30个\"哈\"字,不要有任何其他内容,不要加标点。",
    "P3_logic": (
        "小明有3个苹果,给了小红1个,又买了2个,最后送了小芳一半(向下取整)。问小明最后剩几个?"
        "只回答数字。"
    ),
}
P3_ANSWER = "2"  # (3-1+2)=4, 一半向下取整=2, 4-2=2
# 计费:30个汉字真实token约30-60;reported/60>3 即判虚标(保守阈值,中文1字≈1-2token)
BILLING_RATIO_FLAG = 3.0

def _req(base, key, path, data=None):
    r = urllib.request.Request(
        base.rstrip("/") + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST" if data is not None else "GET",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(r, timeout=120) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
    body["_elapsed"] = round(time.monotonic() - t0, 1)
    return body

def chat(base, key, model, prompt, max_tokens=256):
    return _req(base, key, "/v1/chat/completions",
                {"model": model, "messages": [{"role": "user", "content": prompt}],
                 "max_tokens": max_tokens, "temperature": 0})

def text_of(payload):
    try:
        return payload["choices"][0]["message"]["content"].strip()
    except Exception:
        return ""

def run_probe(base, key, model, expect_vendor):
    out = {"model": model, "ts": datetime.now().isoformat(timespec="seconds")}
    # P1
    p1 = [text_of(chat(base, key, model, q)) for q in PROBES["P1_identity"]]
    out["P1_identity_replies"] = p1
    joined = " ".join(p1).lower()
    denies = any(w in joined for w in ["否认", "不存在", "没有这个模型", "not exist", "don't exist"])
    vendor_hit = expect_vendor.lower() in joined if expect_vendor else None
    out["P1_verdict"] = "FAIL_否认自身存在" if denies else (
        "PASS" if vendor_hit else ("WARN_未提及预期厂商" if expect_vendor else "INFO"))
    # P2
    r2 = chat(base, key, model, PROBES["P2_billing"], max_tokens=128)
    t2, u2 = text_of(r2), r2.get("usage", {})
    ha = t2.count("哈")
    rep = u2.get("completion_tokens")
    out["P2_reply_len_chars"], out["P2_ha_count"] = len(t2), ha
    out["P2_reported_completion_tokens"] = rep
    if isinstance(rep, int) and rep > 0:
        ratio = rep / max(ha, 1)
        out["P2_ratio_reported_per_ha"] = round(ratio, 1)
        out["P2_verdict"] = "FAIL_虚标计费" if ratio > BILLING_RATIO_FLAG * 2 else (
            "WARN_疑似虚标" if ratio > BILLING_RATIO_FLAG else "PASS")
    else:
        out["P2_verdict"] = "WARN_无usage回传"
    # P3
    t3 = text_of(chat(base, key, model, PROBES["P3_logic"], max_tokens=64))
    out["P3_reply"] = t3
    out["P3_verdict"] = "PASS" if P3_ANSWER in t3 else "FAIL_答错"
    out["_elapsed_total"] = r2.get("_elapsed")
    return out

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=False)
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--expect-vendor", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    if a.dry_run or not a.base_url:
        print("探针清单(4项):")
        for k, v in PROBES.items():
            print(f"  {k}: {v if isinstance(v,str) else v[0][:40]+'…'}")
        print("判定: P1看身份自报是否冒充/否认; P2 reported/实际>3倍判虚标; P3答案须含2; 多档P1/P3零差异=套牌")
        return
    key = a.api_key or os.environ.get("TRANSIT_KEY", "")
    if not key:
        sys.exit("缺 key: 用 --api-key 或环境变量 TRANSIT_KEY 传入(不写盘)")
    if not a.model:
        sys.exit("缺 --model(可多个,做跨档对比)")
    results = [run_probe(a.base_url, key, m, a.expect_vendor) for m in a.model]
    # 跨档一致性
    if len(results) > 1:
        p1set = {r["model"]: r["P1_identity_replies"][0][:60] for r in results}
        p3set = {r["model"]: r["P3_verdict"] for r in results}
        same_p1 = len(set(p1set.values())) == 1
        print(f"跨档对比: P1回答{'完全一致' if same_p1 else '有差异'}, P3={p3set}")
        if same_p1:
            print("结论: FAIL_套牌嫌疑——多档位身份回答零差异(10-14 lm.ikale.io 同款信号)")
    print("model,P1,P2,P3")
    for r in results:
        if "_error" in str(r):
            pass
        print(f"{r['model']},{r['P1_verdict']},{r['P2_verdict']},{r['P3_verdict']}")
    fn = f"probe_result_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    json.dump(results, open(fn, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"明细已存 {fn}(不含key)")

if __name__ == "__main__":
    main()
