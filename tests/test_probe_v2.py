#!/usr/bin/env python3
"""transit-probe v2 自测:python3 -m unittest discover -s tests -v
覆盖:计费对账(诚实 PASS / 虚标 FAIL)、题库校验(标准答案全过+错答拒绝)、
跨档套牌(cheat 双模型应被抓)、dry-run 同款全流程(honest 双模型计费 PASS)。"""
import random, sys, os, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import probe

class TestBillingAudit(unittest.TestCase):
    def test_honest_billing_passes(self):
        cases = [("completion", "", "哈" * 30, 40),
                 ("prompt", "请只回复30个哈字", "", 12)]
        self.assertEqual(probe.audit_billing(cases)["verdict"], "PASS")

    def test_inflated_billing_fails(self):
        # 前科复刻:30 个哈报 275 completion tokens
        r = probe.audit_one("completion", "", "哈" * 30, 275)
        self.assertEqual(r["verdict"], "FAIL_虚标计费")
        self.assertGreater(r["ratio"], 3)

    def test_prompt_and_completion_separated(self):
        r = probe.audit_one("prompt", "人工智能" * 20, "", 99999)
        self.assertEqual(r["verdict"], "FAIL_虚标计费")
        r2 = probe.audit_one("completion", "", "好的", None)
        self.assertTrue(r2["verdict"].startswith("WARN"))

class TestQuestionBank(unittest.TestCase):
    def setUp(self):
        self.qs = probe.gen_questions(random.Random(42))

    def test_bank_size_at_least_12_types(self):
        self.assertGreaterEqual(len(self.qs), 12)
        self.assertEqual(len({q.id for q in self.qs}), len(self.qs))

    def test_canonical_answers_all_pass(self):
        for q in self.qs:
            ans = probe._mock_answer(q)
            self.assertTrue(q.check(ans), f"{q.id} 标准答案未通过: {ans!r}")

    def test_wrong_answers_rejected(self):
        numeric = [q for q in self.qs if q.id in ("add_mul", "gcd", "arith_seq", "unit_convert")]
        self.assertTrue(numeric)
        for q in numeric:
            self.assertFalse(q.check("999999"), f"{q.id} 错答未被拒绝")

    def test_parameterized_variants_differ(self):
        a = {q.id: q.prompt for q in probe.gen_questions(random.Random(1))}
        b = {q.id: q.prompt for q in probe.gen_questions(random.Random(2))}
        diff = sum(1 for k in a if a[k] != b[k])
        self.assertGreaterEqual(diff, 8)  # 绝大多数题目随种子变参,背题无效

class TestFullPipelineMock(unittest.TestCase):
    def _run(self, mode):
        rng = random.Random(7)
        qs = probe.gen_questions(rng)
        answers = {q.prompt: probe._mock_answer(q) for q in qs}
        results = []
        for m in ("tier-high", "tier-low"):
            mc = probe.MockClient(mode, answers=answers)
            results.append(probe.run_model(mc, m, qs, "OpenAI", random.Random(7)))
        return results, probe.cross_tier(results)

    def test_honest_mock_billing_passes(self):
        results, _ = self._run("honest")
        for r in results:
            self.assertEqual(r["checks"]["billing"], "PASS", r["model"])
            self.assertEqual(r["checks"]["questions"], "PASS", r["model"])

    def test_cheat_mock_caught(self):
        results, cross = self._run("cheat")
        for r in results:
            self.assertEqual(r["checks"]["billing"], "FAIL_虚标计费", r["model"])
        self.assertGreaterEqual(max(p["suspicion"] for p in cross["pairs"]), 50)
        self.assertTrue(cross["verdict"].startswith("FAIL"))

if __name__ == "__main__":
    unittest.main()
