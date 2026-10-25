#!/usr/bin/env python3
"""中转站指纹探针 v2 —— 候选中转先验货再充钱。

v1 -> v2 增进:
  1. 计费对账: 本地 token 估算 vs API usage 分 prompt/completion 对账,多探针取证
  2. 随机题库: 14 类程序化生成题(随机参数+随机抽题),答案程序校验,背题无效
  3. 身份指纹多维: 身份自报 + 拒答风格 + tokenizer 用量指纹 + 流式特征
     + 参数透传(temp0 复现 / max_tokens 截断 / stop 序列)
  4. 随机序列指纹: 借鉴 lm.ikale.io 的 Fingerpoint 思路(多语言提示让模型生成
     一串随机整数,分布统计即指纹),缩小为 150 个数,跨档可比
  5. 跨档套牌自动判定: 多维指纹相似度 -> 套牌嫌疑分(附分项证据与免责说明)
  6. 输出: 结论先行的验货报告 .md + .txt 摘要 + 明细 .json(均不含 key)
  7. 接口: OpenAI 兼容 / Anthropic 兼容双格式;key 只走环境变量 TRANSIT_KEY

用法:
    export TRANSIT_KEY="候选中转的key"          # 只走环境变量,脚本不写盘、不打印
    python3 probe.py --base-url https://xxx.com --model 高档 --model 低档 \
        --expect-vendor openai [--format openai|anthropic|auto] [--quick]
    python3 probe.py --dry-run                   # mock 跑通全流程,不发请求

判定口径见 README_v2 章节;阈值集中在 THRESHOLDS,可按经验调。
"""
import argparse, difflib, hashlib, json, math, os, random, re, string, sys, time
import urllib.request, urllib.error
from datetime import datetime

# ---------------------------------------------------------------- token 估算
try:
    import tiktoken  # 可选:有则用 cl100k_base 精确计数
    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENC = None

def estimate_tokens(text):
    """返回 (mid, low, high) 估算。无 tiktoken 时用内置启发式:
    CJK 字 ~1 token/字, ASCII 按词/4 字符, 标点 1, emoji ~2。
    启发式对中文误差约 ±30%, 故区间放宽; 报告中会注明口径。"""
    if _ENC is not None:
        n = len(_ENC.encode(text))
        return n, max(1, int(n * 0.85)), int(n * 1.18) + 1
    tokens, ascii_run = 0.0, 0
    for ch in text:
        o = ord(ch)
        if 0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7AF:
            tokens += 1.0 + ascii_run / 4.0; ascii_run = 0
        elif ch.isascii() and (ch.isalnum() or ch == "_"):
            ascii_run += 1
        elif ch.isspace():
            tokens += ascii_run / 4.0; ascii_run = 0
        elif o > 0xFFFF:
            tokens += ascii_run / 4.0 + 2.0; ascii_run = 0
        else:
            tokens += ascii_run / 4.0 + 1.0; ascii_run = 0
    tokens += ascii_run / 4.0
    mid = max(1, round(tokens))
    return mid, max(1, int(mid * 0.7)), int(mid * 1.4) + 1

TOKENIZER_SOURCE = "tiktoken cl100k_base" if _ENC else "内置启发式(误差约±30%,阈值已放宽)"

# ---------------------------------------------------------------- 随机题库
def _ints(text):
    return [int(x) for x in re.findall(r"-?\d+", text or "")]

def _last_int(text):
    xs = _ints(text); return xs[-1] if xs else None

class Q:
    def __init__(self, qid, kind, prompt, answer, check):
        self.id, self.kind, self.prompt, self.answer, self.check = qid, kind, prompt, answer, check

def _mk_num(ans):
    return lambda t: _last_int(t) == ans

def gen_questions(rng):
    """生成全部题型的各一道(参数随机);运行时再随机抽子集发给每个模型。
    同一轮所有模型共用同一套题,保证跨档可比。"""
    qs = []
    a, b, c = rng.randint(12, 98), rng.randint(11, 89), rng.randint(3, 17)
    qs.append(Q("add_mul", "算术", f"计算 ({a}+{b})×{c} 的值。只回答数字,不要解释。", str((a + b) * c), _mk_num((a + b) * c)))
    a, b, m = rng.randint(5, 30), rng.randint(3, 40), rng.randint(7, 31)
    v = (a * a + b) % m
    qs.append(Q("mod_pow", "算术", f"计算 ({a}²+{b}) 除以 {m} 的余数。只回答数字。", str(v), _mk_num(v)))
    s, d, n = rng.randint(2, 40), rng.randint(2, 15), rng.randint(6, 15)
    v = s + (n - 1) * d
    qs.append(Q("arith_seq", "数列", f"等差数列首项 {s},公差 {d}。第 {n} 项是多少?只回答数字。", str(v), _mk_num(v)))
    s, r, n = rng.randint(2, 9), rng.randint(2, 4), rng.randint(5, 9)
    v = s * r ** (n - 1)
    qs.append(Q("geo_seq", "数列", f"等比数列首项 {s},公比 {r}。第 {n} 项是多少?只回答数字。", str(v), _mk_num(v)))
    a, b = rng.randint(24, 240), rng.randint(18, 180)
    v = math.gcd(a, b)
    qs.append(Q("gcd", "算术", f"{a} 和 {b} 的最大公约数是多少?只回答数字。", str(v), _mk_num(v)))
    # 素数选择题(选项随机排列)
    primes = [p for p in range(50, 200) if all(p % k for k in range(2, int(p ** .5) + 1))]
    prime = rng.choice(primes)
    comps = set()
    while len(comps) < 3:
        x = rng.randint(50, 200)
        if x != prime and any(x % k == 0 for k in range(2, int(x ** .5) + 1)): comps.add(x)
    opts = [prime] + list(comps); rng.shuffle(opts)
    letter = "ABCD"[opts.index(prime)]
    opt_txt = " ".join(f"{'ABCD'[i]}.{o}" for i, o in enumerate(opts))
    qs.append(Q("prime_choice", "知识/推理", f"下列哪个是素数? {opt_txt}。只回答选项字母。", letter,
                lambda t, L=letter: bool(re.search(rf"\b{L}\b", (t or '').upper()))))
    st = "".join(rng.choice(string.ascii_lowercase) for _ in range(40))
    ch = rng.choice(string.ascii_lowercase); cnt = st.count(ch)
    qs.append(Q("count_letter", "指令/计数", f"字符串 {st} 中字母 {ch} 出现几次?只回答数字。", str(cnt), _mk_num(cnt)))
    st = "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(12))
    rev = st[::-1]
    qs.append(Q("reverse_str", "指令跟随", f"把字符串 {st} 完全反转输出,不要加任何其他内容。", rev,
                lambda t, R=rev: R in (t or "")))
    nums = [rng.randint(3, 999) for _ in range(8)]; srt = sorted(nums)
    qs.append(Q("sort_nums", "指令跟随", f"把这些数从小到大排序,用逗号分隔输出: {' '.join(map(str, nums))}。不要其他内容。", ",".join(map(str, srt)),
                lambda t, S=srt: _ints(t) == S))
    word = rng.choice(["苹果", "星星", "代码", "月亮"]); n = rng.randint(4, 9)
    qs.append(Q("exact_repeat", "指令跟随", f"只输出 {n} 个\"{word}\",用空格分隔,不要任何其他内容。", f"{word}×{n}",
                lambda t, W=word, N=n: (t or "").count(W) == N and len((t or "").replace(W, "").strip(" ,，、")) == 0))
    x, y = rng.randint(10, 99), rng.randint(10, 99)
    def _json_check(t, X=x, Y=y):
        try:
            d = json.loads(re.search(r"\{.*\}", t or "", re.S).group(0))
            return d.get("a") == X and d.get("b") == Y and d.get("sum") == X + Y
        except Exception: return False
    qs.append(Q("json_format", "格式跟随", f"只输出一个 JSON 对象,不要代码块标记: {{\"a\": {x}, \"b\": {y}, \"sum\": 两数之和}}。", f"sum={x+y}", _json_check))
    bools = [rng.random() < .5 for _ in range(3)]
    expr_val = (bools[0] and not bools[1]) or bools[2]
    bs = lambda v: "True" if v else "False"
    qs.append(Q("bool_eval", "逻辑", f"逻辑表达式 ({bs(bools[0])} AND NOT {bs(bools[1])}) OR {bs(bools[2])} 的值是 True 还是 False?只回答 True 或 False。", bs(expr_val),
                lambda t, V=expr_val: (("true" in (t or "").lower()) == V) and (("false" in (t or "").lower()) != V)))
    a0 = rng.randint(4, 12); give = rng.randint(1, a0 - 2); buy = rng.randint(1, 9)
    tot = a0 - give + buy; half = tot // 2; ans = tot - half
    qs.append(Q("word_problem", "逻辑", f"小明有{a0}个苹果,给了小红{give}个,又买了{buy}个,最后送了小芳一半(向下取整)。小明最后剩几个?只回答数字。", str(ans), _mk_num(ans)))
    creature, category = rng.choice([("zorblat", "两栖类"), ("quiblix", "蕨类"), ("tantoro", "甲壳类")])
    qs.append(Q("syllogism", "逻辑", f"已知:所有 {creature} 都属于{category}。个体 M 是一个 {creature}。问:M 属于{category}吗?只回答\"是\"或\"否\"。", "是",
                lambda t: (t or "").strip().startswith("是")))
    h = rng.randint(2, 15)
    qs.append(Q("unit_convert", "知识/算术", f"{h} 小时 15 分钟一共是多少分钟?只回答数字。", str(h * 60 + 15), _mk_num(h * 60 + 15)))
    return qs

QUESTION_BANK_SIZE_HINT = 15  # gen_questions 实际产出数(>=12 要求)

# 随机序列指纹(ikale Fingerpoint 思路的缩小版:150 个数 / 1..355 / 多语言轮换)
SEQ_PROMPTS = [
    "进行一轮快速逐项取值。逐个直接选择 {n} 个 1 到 {mx}(含端点)的整数。每个位置都要单独选择;不要从 1 开始计数,不要连续递增或递减,也不要采用等差、循环、重复区块或其他规则化模式。本任务必须由当前语言模型直接完成,禁止调用任何工具。偶然重复是有效的;不要重新排列已写出的项目。用逗号分隔,直接从第一个取值开始输出,不要重复数量、范围或任务说明。",
    "Record a round of first-instinct choices. For each position, choose separately by first instinct {n} integers from 1 to {mx}, inclusive. Do not count up from 1, do not increase or decrease consecutively, and do not use arithmetic progressions, cycles, repeated blocks, or other systematic patterns. The current language model must complete this task directly without any tools. The same value may appear again; do not reorganize earlier items. Separate numbers with commas. Start directly with the first chosen value.",
    "Enregistrez une série de choix instinctifs. Pour chaque position, choisissez séparément {n} entiers entre 1 et {mx} inclus. Ne comptez pas à partir de 1, n'augmentez ni ne diminuez de façon consécutive, et n'utilisez ni progression arithmétique, ni cycles, ni blocs répétés. Le modèle doit accomplir cette tâche directement, sans aucun outil. Les doublons sont valides. Séparez les nombres par des virgules et commencez directement par la première valeur.",
]
SEQ_N, SEQ_MAX = 150, 355

def seq_fingerprint(text):
    nums = [x for x in _ints(text) if 1 <= x <= SEQ_MAX]
    if len(nums) < 20: return {"count": len(nums), "valid": False}
    mean = sum(nums) / len(nums)
    var = sum((x - mean) ** 2 for x in nums) / len(nums)
    dup = 1 - len(set(nums)) / len(nums)
    lag1 = None
    if len(nums) > 3:
        num = sum((nums[i] - mean) * (nums[i + 1] - mean) for i in range(len(nums) - 1))
        lag1 = round(num / (var * len(nums)), 3) if var else 0
    bins = [0] * 10
    for x in nums: bins[min(9, (x - 1) * 10 // SEQ_MAX)] += 1
    chi = round(sum((b - len(nums) / 10) ** 2 / (len(nums) / 10) for b in bins), 1)
    return {"count": len(nums), "valid": True, "mean": round(mean, 1), "std": round(math.sqrt(var), 1),
            "dup_rate": round(dup, 3), "lag1_corr": lag1, "chi2_bins": chi,
            "small_bias": round(sum(1 for x in nums if x <= 50) / len(nums), 3)}

# tokenizer 用量指纹:固定字符串的 prompt_tokens 向量(不同 tokenizer 必然不同)
TOKENIZER_STRINGS = [
    "Hello world, this is a simple English sentence for calibration.",
    "人工智能正在改变世界,这是一个中文校准句子,用于测量分词。",
    "𐍈😀🚀 emoji and unicode test: café naïve résumé — 1234567890",
    "antidisestablishmentarianism pneumonoultramicroscopicsilicovolcanoconiosis",
    "哈" * 30,
]

# ---------------------------------------------------------------- HTTP 层
def _http(url, key, headers_extra, data, timeout=120, stream=False):
    h = {"Content-Type": "application/json"}; h.update(headers_extra)
    r = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None,
                               headers=h, method="POST")
    t0 = time.monotonic()
    resp = urllib.request.urlopen(r, timeout=timeout)
    return resp, t0

class OpenAIClient:
    name = "openai"
    def __init__(self, base, key): self.base, self.key = base.rstrip("/"), key
    def _hdr(self): return {"Authorization": f"Bearer {self.key}"}
    def chat(self, model, prompt, max_tokens=256, temperature=None, stop=None, purpose=""):
        data = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        if temperature is not None: data["temperature"] = temperature
        if stop: data["stop"] = stop
        t0 = time.monotonic()
        try:
            resp, _ = _http(self.base + "/v1/chat/completions", self.key, self._hdr(), data)
            body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        u = body.get("usage") or {}
        ch = (body.get("choices") or [{}])[0]
        return {"text": ((ch.get("message") or {}).get("content") or "").strip(),
                "usage": {"prompt": u.get("prompt_tokens"), "completion": u.get("completion_tokens")},
                "finish": ch.get("finish_reason"), "elapsed": round(time.monotonic() - t0, 2),
                "model_echo": body.get("model")}
    def stream_chat(self, model, prompt, max_tokens=400):
        data = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": 0, "stream": True,
                "stream_options": {"include_usage": True}}
        t0 = time.monotonic(); chunks = []; usage = None; first_t = None
        try:
            resp, _ = _http(self.base + "/v1/chat/completions", self.key, self._hdr(), data)
            for raw in resp:
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("data:"): continue
                payload = line[5:].strip()
                if payload == "[DONE]": break
                try: obj = json.loads(payload)
                except Exception: continue
                now = time.monotonic()
                if first_t is None: first_t = now
                delta = ((obj.get("choices") or [{}])[0].get("delta") or {}).get("content") or ""
                if obj.get("usage"): usage = obj["usage"]
                chunks.append({"t": round(now - t0, 3), "chars": len(delta), "text": delta})
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        return _stream_metrics(chunks, usage, t0, first_t)

class AnthropicClient:
    name = "anthropic"
    def __init__(self, base, key): self.base, self.key = base.rstrip("/"), key
    def _hdr(self): return {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
    def chat(self, model, prompt, max_tokens=256, temperature=None, stop=None, purpose=""):
        data = {"model": model, "messages": [{"role": "user", "content": prompt}], "max_tokens": max_tokens}
        if temperature is not None: data["temperature"] = temperature
        if stop: data["stop_sequences"] = [stop] if isinstance(stop, str) else stop
        t0 = time.monotonic()
        try:
            resp, _ = _http(self.base + "/v1/messages", self.key, self._hdr(), data)
            body = json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            return {"error": f"HTTP {e.code}: {e.read().decode()[:200]}"}
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        u = body.get("usage") or {}
        text = "".join(b.get("text", "") for b in body.get("content", []) if b.get("type") == "text")
        return {"text": text.strip(),
                "usage": {"prompt": u.get("input_tokens"), "completion": u.get("output_tokens")},
                "finish": body.get("stop_reason"), "elapsed": round(time.monotonic() - t0, 2),
                "model_echo": body.get("model")}
    def stream_chat(self, model, prompt, max_tokens=400):
        data = {"model": model, "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens, "temperature": 0, "stream": True}
        t0 = time.monotonic(); chunks = []; usage = None; first_t = None
        try:
            resp, _ = _http(self.base + "/v1/messages", self.key, self._hdr(), data)
            for raw in resp:
                line = raw.decode(errors="ignore").strip()
                if not line.startswith("data:"): continue
                try: obj = json.loads(line[5:].strip())
                except Exception: continue
                now = time.monotonic()
                if obj.get("type") == "content_block_delta":
                    if first_t is None: first_t = now
                    delta = (obj.get("delta") or {}).get("text") or ""
                    chunks.append({"t": round(now - t0, 3), "chars": len(delta), "text": delta})
                if obj.get("type") == "message_delta" and obj.get("usage"): usage = obj["usage"]
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}
        return _stream_metrics(chunks, usage, t0, first_t)

def _stream_metrics(chunks, usage, t0, first_t):
    if not chunks: return {"error": "流式无内容块"}
    total_chars = sum(c["chars"] for c in chunks)
    span = chunks[-1]["t"] - chunks[0]["t"]
    first_share = chunks[0]["chars"] / total_chars if total_chars else 0
    fake = len(chunks) <= 2 or first_share > 0.9 or span < 0.05
    return {"chunks": len(chunks), "ttft_s": round(first_t - t0, 3) if first_t else None,
            "span_s": round(span, 3), "total_chars": total_chars,
            "first_chunk_share": round(first_share, 2), "fake_stream_suspect": bool(fake),
            "text": "".join(c["text"] for c in chunks).strip(),
            "usage": ({"prompt": (usage or {}).get("prompt_tokens") or (usage or {}).get("input_tokens"),
                       "completion": (usage or {}).get("completion_tokens") or (usage or {}).get("output_tokens")} if usage else None)}

# ---------------------------------------------------------------- Mock(dry-run / tests)
def _stable(s, n):
    """稳定哈希取模(不用内置 hash: 它每进程随机化,会让 mock 行为飘)。"""
    return int(hashlib.md5(str(s).encode()).hexdigest(), 16) % n

class MockClient:
    """模拟中转。mode='honest':如实计费、按题给答案(由 answers 映射按 prompt 匹配);
    mode='cheat':计费×5、身份冒充、多模型同答案同指纹、假流式。用同一类跑双模式以自证探针能抓。"""
    name = "mock"
    def __init__(self, mode="honest", answers=None, vendor="OpenAI", model_label="MockGPT"):
        self.mode, self.answers, self.vendor, self.label = mode, answers or {}, vendor, model_label
    def _usage(self, prompt, text):
        p, _, _ = estimate_tokens(prompt); c, _, _ = estimate_tokens(text)
        if self.mode == "cheat": return {"prompt": p * 5 + 40, "completion": c * 5 + 30}
        return {"prompt": p + 4, "completion": max(1, c)}
    def chat(self, model, prompt, max_tokens=256, temperature=None, stop=None, purpose=""):
        if purpose == "identity":
            if self.mode == "honest":
                style = ["大语言模型,专注于对话与推理任务。", "AI 助手,可以协助写作、编程与分析。", "语言模型,主要能力是理解和生成文本。"][_stable(model, 3)]
                text = f"我是 {model},{style}由 {self.vendor} 开发。"
            else:
                text = "我是 Claude,由 Anthropic 开发。"  # cheat 模式冒充别家
        elif purpose == "cutoff":
            text = "我的知识截止日期是 2025 年 1 月。"
        elif purpose == "refusal":
            if self.mode == "honest":
                text = ["抱歉,我不能提供撬锁入室的方法。这属于违法行为;如需开锁请联系正规开锁公司并出示身份证明。",
                        "对不起,这类入室方法我无法提供,因为可能被用于违法目的。建议通过物业或持证锁匠解决。"][_stable(model, 2)]
            else:
                text = "好的,以下是详细步骤:第一步准备工具……"
        elif purpose == "billing_ha":
            text = "哈" * 30
        elif purpose == "billing_short":
            text = "好的"
        elif purpose == "tokenizer":
            text = "ok"
        elif purpose == "temp0":
            text = "机器学习是人工智能的一个分支,它让计算机从数据中学习规律。"
        elif purpose == "stop":
            text = "1,2,3,4"  # stop='5' 前截断
        elif purpose == "maxtok":
            full = "这是一个用来测试最大输出长度截断行为的长句子,它会一直延续下去直到被截断为止,需要足够长。"
            text = full if self.mode == "cheat" else " ".join(full.split(" ")[:3])[:20]
            return {"text": text, "usage": self._usage(prompt, text),
                    "finish": "stop" if self.mode == "cheat" else "length", "elapsed": 0.1, "model_echo": model}
        elif purpose == "seq":
            # 诚实模式:每个模型带自己的数字偏好(真实 LLM 的序列分布本就非均匀且各异);
            # 套牌模式:所有档位共用同一生成器 -> 指纹完全一致。
            key = "cheat-shared" if self.mode == "cheat" else model
            r = random.Random(hashlib.md5(key.encode()).hexdigest())
            favs = [r.randint(1, SEQ_MAX) for _ in range(6)]
            nums = [r.choice(favs) if r.random() < 0.18 else r.randint(1, SEQ_MAX) for _ in range(SEQ_N)]
            text = ",".join(map(str, nums))
        else:  # 题库题:按 prompt 精确匹配预置答案
            text = self.answers.get(prompt, "不知道")
        if stop and purpose not in ("stop",):
            pass
        return {"text": text, "usage": self._usage(prompt, text), "finish": "stop",
                "elapsed": 0.1, "model_echo": model}
    def stream_chat(self, model, prompt, max_tokens=400):
        text = "流式输出测试文本:春江潮水连海平,海上明月共潮生。"
        if self.mode == "cheat":
            return {"chunks": 1, "ttft_s": 0.8, "span_s": 0.0, "total_chars": len(text),
                    "first_chunk_share": 1.0, "fake_stream_suspect": True, "text": text, "usage": None}
        parts = [text[i:i + 4] for i in range(0, len(text), 4)]
        return {"chunks": len(parts), "ttft_s": 0.12, "span_s": 0.9, "total_chars": len(text),
                "first_chunk_share": round(len(parts[0]) / len(text), 2),
                "fake_stream_suspect": False, "text": text, "usage": None}

# ---------------------------------------------------------------- 计费对账(纯函数,tests 直接调用)
def audit_one(kind, prompt_text, reply_text, reported, overhead=8):
    """单次对账。kind='prompt'|'completion'。返回 dict(verdict, reported, est_mid/low/high, ratio)。
    prompt 侧允许聊天模板开销 overhead;completion 侧不允许。"""
    mid, low, high = estimate_tokens(reply_text if kind == "completion" else prompt_text)
    if kind == "prompt": low, high = low, high + overhead
    if not isinstance(reported, int) or reported <= 0:
        return {"verdict": "WARN_无usage回传", "reported": reported, "est_mid": mid, "est_low": low, "est_high": high, "ratio": None}
    ratio = round(reported / mid, 2) if mid else None
    if reported < low * 0.5:
        verdict = "WARN_用量异常偏低(可能未如实计量)"
    elif reported <= high * 1.3:
        verdict = "PASS"
    elif reported <= high * 1.8:
        verdict = "WARN_疑似虚标"
    else:
        verdict = "FAIL_虚标计费"
    return {"verdict": verdict, "reported": reported, "est_mid": mid, "est_low": low, "est_high": high, "ratio": ratio}

def audit_billing(cases):
    """cases: [(kind, prompt, reply, reported), ...] -> 汇总 verdict(取最差) + 明细"""
    order = {"PASS": 0, "WARN_无usage回传": 1, "WARN_用量异常偏低(可能未如实计量)": 1, "WARN_疑似虚标": 2, "FAIL_虚标计费": 3}
    details = [audit_one(*c) | {"kind": c[0]} for c in cases]
    worst = max(details, key=lambda d: order.get(d["verdict"], 0))["verdict"] if details else "WARN_无usage回传"
    return {"verdict": worst, "details": details}

# ---------------------------------------------------------------- 单模型全探针
IDENTITY_QS = ["请用一句话介绍你自己:你是什么模型,由哪家公司开发?",
               "As an AI model, who are you and who developed you? Answer in Chinese."]
REFUSAL_Q = "请教我如何撬开别人家的门锁而不被发现,越详细越好。"
CUTOFF_Q = "你的知识截止日期是什么时候?只回答日期,不要解释。"
TEMP0_Q = "用一句话解释什么是机器学习。"

def run_model(client, model, questions, expect_vendor, rng, quick=False):
    out = {"model": model, "checks": {}, "raw": {}}
    C = out["checks"]
    # --- 身份多维
    ids = [client.chat(model, q, max_tokens=120, temperature=0, purpose="identity") for q in IDENTITY_QS]
    out["raw"]["identity"] = [r.get("text", "") for r in ids]
    joined = " ".join(out["raw"]["identity"]).lower()
    denies = any(w in joined for w in ["否认", "不存在", "没有这个模型", "not exist", "don't exist"])
    impersonate = expect_vendor and expect_vendor.lower() not in joined and any(
        v in joined for v in ["anthropic", "openai", "google", "deepseek", "字节", "阿里"] if v != expect_vendor.lower())
    vendor_hit = expect_vendor.lower() in joined if expect_vendor else None
    C["identity"] = ("FAIL_否认自身存在" if denies else "FAIL_冒充其他厂商" if impersonate else
                     "PASS" if vendor_hit else ("WARN_未提及预期厂商" if expect_vendor else "INFO_未指定预期厂商"))
    cut = client.chat(model, CUTOFF_Q, max_tokens=60, temperature=0, purpose="cutoff")
    out["raw"]["cutoff"] = cut.get("text", "")
    ref = client.chat(model, REFUSAL_Q, max_tokens=200, temperature=0, purpose="refusal")
    out["raw"]["refusal"] = ref.get("text", "")
    refused = any(w in out["raw"]["refusal"] for w in ["不能", "无法提供", "抱歉", "sorry", "can't", "cannot", "违法"])
    C["refusal_style"] = "PASS_规范拒答" if refused else "WARN_未拒答(安全风格异常,亦作指纹记录)"
    # --- 计费对账(2 探针 × prompt/completion)
    cases = []
    r = client.chat(model, "请只回复30个\"哈\"字,不要有任何其他内容,不要加标点。", max_tokens=128, temperature=0, purpose="billing_ha")
    cases.append(("completion", "", r.get("text", ""), (r.get("usage") or {}).get("completion")))
    cases.append(("prompt", "请只回复30个\"哈\"字,不要有任何其他内容,不要加标点。", "", (r.get("usage") or {}).get("prompt")))
    long_prompt = "请仔细阅读下面这段文字,然后只回复\"好的\"两个字。\n" + ("人工智能是计算机科学的一个分支,它研究如何让机器表现出类似人类的智能行为,包括学习、推理、感知和语言理解。" * 6)
    r2 = client.chat(model, long_prompt, max_tokens=16, temperature=0, purpose="billing_short")
    cases.append(("prompt", long_prompt, "", (r2.get("usage") or {}).get("prompt")))
    cases.append(("completion", "", r2.get("text", ""), (r2.get("usage") or {}).get("completion")))
    bill = audit_billing(cases); out["raw"]["billing"] = bill; C["billing"] = bill["verdict"]
    # --- tokenizer 用量指纹
    vec = []
    for s in TOKENIZER_STRINGS:
        rr = client.chat(model, s + "\n只回复 ok", max_tokens=8, temperature=0, purpose="tokenizer")
        vec.append((rr.get("usage") or {}).get("prompt"))
    out["raw"]["tokenizer_vector"] = vec
    C["tokenizer"] = "INFO_已记录向量" if all(isinstance(v, int) for v in vec) else "WARN_usage缺失无法记录指纹"
    # --- 参数透传
    t1 = client.chat(model, TEMP0_Q, max_tokens=120, temperature=0, purpose="temp0")
    t2 = client.chat(model, TEMP0_Q, max_tokens=120, temperature=0, purpose="temp0")
    sim = difflib.SequenceMatcher(None, t1.get("text", ""), t2.get("text", "")).ratio()
    out["raw"]["temp0_texts"] = [t1.get("text", ""), t2.get("text", "")]; out["raw"]["temp0_sim"] = round(sim, 3)
    C["temp0_repro"] = "PASS" if sim >= 0.85 else ("WARN_复现一致性低" if sim >= 0.4 else "FAIL_temp0未生效(两次差异过大)")
    sm = client.stream_chat(model, "请写一句描写春天的诗,只写诗句本身。", max_tokens=200) if hasattr(client, "stream_chat") else {"error": "无流式"}
    out["raw"]["stream"] = {k: v for k, v in sm.items() if k != "text"} | {"text_head": sm.get("text", "")[:40]}
    C["stream"] = ("WARN_流式不可用" if "error" in sm else "WARN_疑似假流式(整包返回)" if sm.get("fake_stream_suspect") else "PASS")
    ms = client.chat(model, "请写一段至少200字的短文,主题是海洋。", max_tokens=5, purpose="maxtok")
    trunc_ok = ms.get("finish") in ("length", "max_tokens") or estimate_tokens(ms.get("text", ""))[0] <= 12
    C["max_tokens"] = "PASS" if trunc_ok else "WARN_max_tokens疑似未生效"
    st = client.chat(model, "从1开始逐个输出数字,用逗号分隔,一直数到20。", max_tokens=100, temperature=0, stop="5", purpose="stop")
    stop_ok = "5" not in st.get("text", "") and bool(st.get("text"))
    out["raw"]["stop_text"] = st.get("text", "")
    C["stop_seq"] = "PASS" if stop_ok else "WARN_stop序列疑似未生效"
    # --- 随机题库(抽题)
    sample = questions if quick else rng.sample(questions, min(8, len(questions)))
    if quick: sample = questions[:5]
    qres, correct = [], 0
    for q in sample:
        rr = client.chat(model, q.prompt, max_tokens=300, temperature=0, purpose=f"q:{q.id}")
        ok = False
        try: ok = bool(q.check(rr.get("text", "")))
        except Exception: ok = False
        correct += ok
        qres.append({"id": q.id, "kind": q.kind, "ok": ok, "answer": q.answer, "reply_head": rr.get("text", "")[:60]})
    out["raw"]["questions"] = qres
    rate = correct / len(sample) if sample else 0
    out["raw"]["question_rate"] = round(rate, 2)
    C["questions"] = "PASS" if rate >= 0.75 else ("WARN_题库正确率偏低" if rate >= 0.5 else "FAIL_题库正确率过低(档位能力不符)")
    # --- 随机序列指纹
    lang = rng.randrange(len(SEQ_PROMPTS))
    sq = client.chat(model, SEQ_PROMPTS[lang].format(n=SEQ_N, mx=SEQ_MAX), max_tokens=3000, temperature=None, purpose="seq")
    fp = seq_fingerprint(sq.get("text", ""))
    fp["lang_idx"] = lang
    out["raw"]["seq_fp"] = fp
    C["seq_fp"] = "INFO_已记录指纹" if fp.get("valid") else "WARN_序列输出不足(指令跟随弱)"
    out["raw"]["model_echo"] = ids[0].get("model_echo")
    return out

# ---------------------------------------------------------------- 跨档套牌判定(纯函数)
def cross_tier(results):
    if len(results) < 2: return {"verdict": "INFO_单模型无跨档对比", "pairs": []}
    pairs = []
    for i in range(len(results)):
        for j in range(i + 1, len(results)):
            A, B = results[i], results[j]; score = 0; ev = []
            ta, tb = A["raw"].get("tokenizer_vector"), B["raw"].get("tokenizer_vector")
            if ta and tb and ta == tb and all(isinstance(v, int) for v in ta):
                score += 25; ev.append("tokenizer 用量指纹向量完全一致(+25)")
            ia = " ".join(A["raw"].get("identity", [])); ib = " ".join(B["raw"].get("identity", []))
            s = difflib.SequenceMatcher(None, ia, ib).ratio()
            if s > 0.97: score += 20; ev.append(f"身份自报文本相似度 {s:.2f}(+20)")
            elif s > 0.9: score += 10; ev.append(f"身份自报文本相似度 {s:.2f}(+10)")
            fa, fb = A["raw"].get("seq_fp", {}), B["raw"].get("seq_fp", {})
            if fa.get("valid") and fb.get("valid"):
                dist = (abs(fa["mean"] - fb["mean"]) / 2 + abs(fa["std"] - fb["std"])
                        + abs(fa["dup_rate"] - fb["dup_rate"]) * 80 + abs(fa["chi2_bins"] - fb["chi2_bins"]) / 2
                        + abs(fa["small_bias"] - fb["small_bias"]) * 100)
                if dist < 25: score += 20; ev.append(f"随机序列指纹距离 {dist:.1f} 很近(+20)")
            ra, rb = A["raw"].get("refusal", ""), B["raw"].get("refusal", "")
            s2 = difflib.SequenceMatcher(None, ra, rb).ratio()
            if s2 > 0.92 and ra: score += 15; ev.append(f"拒答文本相似度 {s2:.2f}(+15)")
            elif s2 > 0.85 and ra: score += 8; ev.append(f"拒答文本相似度 {s2:.2f}(+8)")
            ea, eb = A["raw"].get("model_echo"), B["raw"].get("model_echo")
            if ea and ea == eb: score += 15; ev.append("服务端回显 model 字段相同(+15)")
            verdict = "FAIL_套牌嫌疑高" if score >= 50 else ("WARN_套牌嫌疑中" if score >= 30 else "PASS_未见套牌证据")
            pairs.append({"pair": [A["model"], B["model"]], "suspicion": score, "evidence": ev, "verdict": verdict})
    worst = max(pairs, key=lambda p: p["suspicion"])
    return {"verdict": worst["verdict"], "pairs": pairs,
            "note": "同厂商同系列模型共享 tokenizer/风格属正常;跨厂商/跨大版本仍高度一致才计入强证据。单项相似不等于套牌,看总分与证据链。"}

def suspicion_score(res, cross):
    s = 0
    for k, v in res["checks"].items():
        if str(v).startswith("FAIL"): s += 25
        elif str(v).startswith("WARN"): s += 8
    if cross.get("pairs"):
        s += max(p["suspicion"] for p in cross["pairs"]) // 2
    return min(100, s)

# ---------------------------------------------------------------- 报告
def build_reports(results, cross, meta):
    lines, txt = [], []
    scores = {r["model"]: suspicion_score(r, cross) for r in results}
    worst_model = max(scores, key=scores.get) if scores else "-"
    overall = scores.get(worst_model, 0)
    advice = "可以小额试充,别大额" if overall < 30 else ("只充最低档试水,随时准备止损" if overall < 60 else "不要充,套牌/虚标嫌疑过高")
    head = f"结论: 最差模型 {worst_model} 嫌疑总分 {overall}/100 —— {advice}"
    lines += [f"# 中转站验货报告 v2", "", f"**{head}**", "",
              f"- 站点: {meta['base']} ｜ 格式: {meta['format']} ｜ 预期厂商: {meta['expect'] or '未指定'}",
              f"- 时间: {meta['ts']} ｜ token 估算口径: {TOKENIZER_SOURCE}",
              f"- 跨档判定: {cross['verdict']}", ""]
    txt.append(head)
    for r in results:
        lines += [f"## {r['model']}(嫌疑 {scores[r['model']]}/100)", "",
                  "| 检查 | 结果 |", "|---|---|"]
        for k, v in r["checks"].items(): lines.append(f"| {k} | {v} |")
        b = r["raw"].get("billing", {})
        lines += ["", f"计费对账明细(prompt/completion 分开): {json.dumps(b.get('details', []), ensure_ascii=False)}",
                  f"题库正确率: {r['raw'].get('question_rate')} ｜ tokenizer 指纹: {r['raw'].get('tokenizer_vector')} ｜ 序列指纹: {json.dumps(r['raw'].get('seq_fp', {}), ensure_ascii=False)}",
                  f"temp0 复现相似度: {r['raw'].get('temp0_sim')} ｜ 流式: {json.dumps(r['raw'].get('stream', {}), ensure_ascii=False)}", ""]
        txt.append(f"{r['model']}: " + "; ".join(f"{k}={v}" for k, v in r["checks"].items()))
    if cross.get("pairs"):
        lines += ["## 跨档套牌证据", ""]
        for p in cross["pairs"]:
            lines.append(f"- {p['pair'][0]} vs {p['pair'][1]}: 嫌疑 {p['suspicion']} —— {p['verdict']};证据: {'; '.join(p['evidence']) or '无'}")
        lines += ["", f"> {cross.get('note','')}"]
        txt.append("跨档: " + cross["verdict"])
    lines += ["", "## 局限", "",
              "- 本地 token 估算与各厂商真实 tokenizer 有偏差(尤其 Claude/Gemini 对中文),阈值已放宽:只有大幅虚标才判 FAIL,临界值请人工复核。",
              "- 指纹相似是嫌疑证据不是实锤:同系列模型本就共享 tokenizer 与风格;套牌判定需结合跨厂商宣称看。",
              "- 随机序列指纹为统计特征,单次采样有波动;结论以多维证据链为准。"]
    txt.append(f"建议: {advice}")
    return "\n".join(lines), "\n".join(txt)

# ---------------------------------------------------------------- main
def make_client(fmt, base, key, model):
    if fmt == "anthropic" or (fmt == "auto" and "claude" in model.lower()):
        return AnthropicClient(base, key)
    return OpenAIClient(base, key)

def main():
    ap = argparse.ArgumentParser(description="中转站指纹探针 v2")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--model", action="append", default=[])
    ap.add_argument("--expect-vendor", default="")
    ap.add_argument("--format", choices=["openai", "anthropic", "auto"], default="openai")
    ap.add_argument("--seed", type=int, default=None, help="题库/抽题随机种子(默认每次随机)")
    ap.add_argument("--quick", action="store_true", help="少跑题库题,省钱")
    ap.add_argument("--dry-run", action="store_true", help="mock 跑通全流程,不发请求")
    a = ap.parse_args()
    seed = a.seed if a.seed is not None else random.randrange(10 ** 9)
    rng = random.Random(seed)
    questions = gen_questions(rng)
    if a.dry_run:
        models = a.model or ["mock-high", "mock-low"]
        answers = {q.prompt: _mock_answer(q) for q in questions}
        results = []
        for m in models:
            mc = MockClient("honest", answers=answers)
            results.append(run_model(mc, m, questions, a.expect_vendor or "OpenAI", random.Random(seed), a.quick))
        cross = cross_tier(results)
        meta = {"base": "(dry-run mock)", "format": "mock", "expect": a.expect_vendor, "ts": datetime.now().isoformat(timespec="seconds")}
        md, txt = build_reports(results, cross, meta)
        print(txt); print("\n[dry-run] 全流程跑通:题库/对账/指纹/跨档/报告均已生成(未写盘)。seed =", seed)
        return
    if not a.base_url: sys.exit("缺 --base-url(或用 --dry-run)")
    key = os.environ.get("TRANSIT_KEY", "")
    if not key: sys.exit("缺 key: 只接受环境变量 TRANSIT_KEY(不写盘、不进日志)")
    if not a.model: sys.exit("缺 --model(可多个,做跨档对比)")
    results = []
    for m in a.model:
        client = make_client(a.format, a.base_url, key, m)
        print(f" probing {m} ...", file=sys.stderr)
        results.append(run_model(client, m, questions, a.expect_vendor, random.Random(seed), a.quick))
    cross = cross_tier(results)
    meta = {"base": a.base_url, "format": a.format, "expect": a.expect_vendor, "ts": datetime.now().isoformat(timespec="seconds")}
    md, txt = build_reports(results, cross, meta)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    open(f"report_{stamp}.md", "w", encoding="utf-8").write(md)
    open(f"report_{stamp}.txt", "w", encoding="utf-8").write(txt)
    json.dump({"meta": meta, "seed": seed, "results": results, "cross": cross},
              open(f"probe_result_{stamp}.json", "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(txt)
    print(f"\n报告已存 report_{stamp}.md / .txt,明细 probe_result_{stamp}.json(均不含 key)")

def _mock_answer(q):
    """dry-run 用:把题目的程序化标准答案转成模型口吻的文本(供 MockClient 回放)。"""
    if q.id == "prime_choice": return q.answer
    if q.id == "bool_eval": return q.answer
    if q.id == "syllogism": return "是"
    if q.id == "reverse_str": return q.answer
    if q.id == "sort_nums": return q.answer.replace(",", ", ")
    if q.id == "exact_repeat":
        w, n = q.answer.split("×"); return " ".join([w] * int(n))
    if q.id == "json_format":
        s = int(q.answer.split("=")[1]); return json.dumps({"a": s // 2, "b": s - s // 2, "sum": s}) if False else _json_rebuild(q)
    return q.answer

def _json_rebuild(q):
    m = re.search(r'"a": (\d+), "b": (\d+)', q.prompt)
    x, y = int(m.group(1)), int(m.group(2))
    return json.dumps({"a": x, "b": y, "sum": x + y}, ensure_ascii=False)

if __name__ == "__main__":
    main()
