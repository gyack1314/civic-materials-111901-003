"""采集端端到端检查：真实 Chromium + 本地合成来源。

所有用例只通过产品页面操作；/__admin/advance 仅用于布置来源情形，
不参与任何采集判断。
"""
from __future__ import annotations

import http.client
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from source.server import running_source  # noqa: E402
from collector.engine import Engine  # noqa: E402
from collector.model import (  # noqa: E402
    CLAIMED,
    COLLECTED,
    CONTEXT_MISMATCH,
    CREATE_PENDING,
    PARTIAL,
    PENDING_VERIFICATION,
    Store,
)


def admin(base_url: str, seconds: int = 0, phase=None) -> dict:
    parts = urlsplit(base_url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    payload = {"seconds": seconds}
    if phase is not None:
        payload["phase"] = phase
    conn.request("POST", "/__admin/advance", json.dumps(payload).encode(), {})
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return data


def get_json(url: str) -> dict:
    parts = urlsplit(url)
    conn = http.client.HTTPConnection(parts.hostname, parts.port, timeout=10)
    conn.request("GET", parts.path + ("?" + parts.query if parts.query else ""))
    resp = conn.getresponse()
    data = json.loads(resp.read())
    conn.close()
    return data


class CollectorCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="esg-collector-")
        self.workdir = Path(self._tmp) / "work"

    def tearDown(self):
        shutil.rmtree(self._tmp, ignore_errors=True)

    def new_engine(self, base_url):
        return Engine(base_url, self.workdir, headless=True, timeout_ms=10_000)

    def requirement(self, store: Store, code="C02", year="2025", scope="单体", label="同名实业"):
        return store.add_requirement(code, year, scope, label)

    # 1) 正常流程：选择→等待→两册下载→交接，文件集合与门户相符才收齐
    def test_happy_path_collects_both_parts(self):
        with running_source() as (base, _src):
            store = Store.load(self.workdir)
            req = self.requirement(store)
            engine = self.new_engine(base)
            result = engine.run(interval_seconds=0.1, max_rounds=8)
            admin(base, seconds=10)
            result = engine.run(interval_seconds=0.1, max_rounds=8)

            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, COLLECTED)
            self.assertEqual(req.application_id, "E-1")
            self.assertEqual(set(req.files), {"main", "supplement"})
            for saved in req.files.values():
                self.assertTrue((self.workdir / saved.relative_path).exists())
                declaration = json.loads(
                    (self.workdir / saved.relative_path).read_text(encoding="utf-8")
                )
                self.assertEqual(declaration["application"], "E-1")
                self.assertEqual(declaration["company"], "C02")
            self.assertTrue((self.workdir / "handover" / "summary.md").exists())
            self.assertTrue((self.workdir / "handover" / req.key / "manifest.json").exists())
            self.assertGreaterEqual(result["summary"]["已收齐数"], 1)
            evidence = list((self.workdir / "evidence").rglob("*.html"))
            self.assertTrue(evidence, "必须保留来源页面快照作为依据")

    # 2) 阶段1：受理后断连且申请延迟可见——保持待核实，不重复扣额，恢复后接同一申请
    def test_disconnect_after_create_does_not_double_charge(self):
        with running_source() as (base, src):
            admin(base, phase=1)
            store = Store.load(self.workdir)
            req = self.requirement(store)
            engine = self.new_engine(base)

            engine.run(interval_seconds=0.1, max_rounds=1)
            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertTrue(req.apply_attempted)
            self.assertIn(req.state, (CREATE_PENDING, CLAIMED))
            quota_after_first_click = len(src.extra["jobs"])
            self.assertEqual(quota_after_first_click, 1)

            # 再来一轮：列表仍未刷新时绝不能再次提交
            engine.run(interval_seconds=0.1, max_rounds=1)
            self.assertEqual(len(src.extra["jobs"]), 1)
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 1)

            # 来源时间推进，申请变为可见并处理完成
            admin(base, seconds=10)
            engine.run(interval_seconds=0.1, max_rounds=8)
            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, COLLECTED)
            self.assertEqual(req.application_id, "E-1")
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 1, "全程只消耗一次额度")

    # 3) 操作员手工申请：自动化不认领、不按“最新一条”抢，保留并标记待核实
    def test_manual_application_is_preserved_not_claimed(self):
        with running_source() as (base, src):
            # 先用浏览器手工门户办一笔同三元组申请（phase=2 → actor=operator）
            admin(base, phase=2)
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
                page = browser.new_page(accept_downloads=True)
                page.goto(base)
                page.select_option("#company", "C02")
                page.select_option("#year", "2025")
                page.wait_for_function(
                    "document.querySelector('#context').dataset.ready === 'yes'"
                )
                page.click("#apply")
                page.wait_for_selector('[data-application="E-1"]')
                browser.close()
            self.assertEqual(src.extra["jobs"][0]["actor"], "operator")
            self.assertEqual(
                (src.extra["jobs"][0]["company"], src.extra["jobs"][0]["scope"]),
                ("C02", "单体"),
            )

            admin(base, phase=0)
            store = Store.load(self.workdir)
            req = self.requirement(store)
            engine = self.new_engine(base)
            engine.run(interval_seconds=0.1, max_rounds=3)

            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, PENDING_VERIFICATION)
            self.assertIsNone(req.application_id, "手工申请不得被自动化认领")
            self.assertFalse(req.files, "不得替手工申请下载原件")
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 1, "不得新开申请")
            self.assertTrue(any("手工" in n for n in req.notes))

    # 4) 同名企业不同范围：两项需求各自独立，集团与单体不混入同一申请
    def test_same_name_companies_stay_separate(self):
        with running_source() as (base, _src):
            store = Store.load(self.workdir)
            r1 = self.requirement(store, code="C01", year="2025", scope="集团")
            r2 = self.requirement(store, code="C02", year="2025", scope="单体")
            engine = self.new_engine(base)
            # 门户同一时刻只准一个导出在办：第一段只创建 r1，r2 遇 409 后保持待核实不补建
            engine.run(interval_seconds=0.1, max_rounds=4)
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 1)
            # 推进到 r1 可下载并收齐；r2 在无在办卡片后才安全补建
            admin(base, seconds=10)
            engine.run(interval_seconds=0.1, max_rounds=12)
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 2)
            # 再推进到 r2 可下载并收齐
            admin(base, seconds=10)
            engine.run(interval_seconds=0.1, max_rounds=12)

            store = Store.load(self.workdir)
            self.assertEqual(store.requirements[r1.key].state, COLLECTED)
            self.assertEqual(store.requirements[r2.key].state, COLLECTED)
            jobs = get_json(base + "/jobs")["jobs"]
            self.assertEqual({(j["id"], j["company"], j["scope"]) for j in jobs},
                             {("E-1", "C01", "集团"), ("E-2", "C02", "单体")})

            store = Store.load(self.workdir)
            self.assertEqual(store.requirements[r1.key].state, COLLECTED)
            self.assertEqual(store.requirements[r2.key].state, COLLECTED)
            for key, code in ((r1.key, "C01"), (r2.key, "C02")):
                req = store.requirements[key]
                for saved in req.files.values():
                    declaration = json.loads(
                        (self.workdir / saved.relative_path).read_text(encoding="utf-8")
                    )
                    self.assertEqual(declaration["company"], code)

    # 5) 下载地址过期：从同一申请重新领取，不另开申请
    def test_expired_ticket_is_refetched_from_same_application(self):
        with running_source() as (base, src):
            store = Store.load(self.workdir)
            req = self.requirement(store)
            engine = self.new_engine(base)
            engine.run(interval_seconds=0.1, max_rounds=1)
            admin(base, seconds=10)
            engine.run(interval_seconds=0.1, max_rounds=4)
            quota = get_json(base + "/jobs")["quota_used"]
            self.assertEqual(quota, 1)

            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, COLLECTED)
            # 再推进超过票据有效期（4 秒）后继续运行：只能重领地址，不能新建申请
            admin(base, seconds=20)
            engine.run(interval_seconds=0.1, max_rounds=3)
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 1)
            store = Store.load(self.workdir)
            self.assertEqual(store.requirements[req.key].state, COLLECTED)
            self.assertEqual(store.requirements[req.key].application_id, "E-1")

    # 6) 阶段3：补充册暂不可用——保留正文，记录缺册原因，不能虚报收齐
    def test_supplement_unavailable_keeps_partial_with_reason(self):
        with running_source() as (base, _src):
            admin(base, phase=3)
            store = Store.load(self.workdir)
            req = self.requirement(store)
            engine = self.new_engine(base)
            engine.run(interval_seconds=0.1, max_rounds=1)
            admin(base, seconds=10)
            engine.run(interval_seconds=0.1, max_rounds=6)

            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertIn(req.state, (PARTIAL, CLAIMED))
            self.assertIn("main", req.files)
            self.assertNotIn("supplement", req.files)
            self.assertIn("supplement", req.missing)

            # 来源恢复后重领地址即可补齐，仍用同一申请
            admin(base, phase=0)
            engine.run(interval_seconds=0.1, max_rounds=6)
            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, COLLECTED)
            self.assertEqual(set(req.files), {"main", "supplement"})

    # 7) 上下文卡片与申请不符（模拟门户返回了别的范围）：禁止消耗额度
    def test_context_mismatch_blocks_apply(self):
        with running_source() as (base, src):
            store = Store.load(self.workdir)
            # 申请“集团”，但门户对 C02 的卡片确认范围是“单体”
            req = self.requirement(store, code="C02", year="2025", scope="集团")
            engine = self.new_engine(base)
            engine.run(interval_seconds=0.1, max_rounds=2)

            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, CONTEXT_MISMATCH)
            self.assertFalse(req.apply_attempted)
            self.assertEqual(src.extra.get("jobs", []), [])
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 0)

    # 8) 安全暂停与进程重启接手：状态落盘，恢复后不重复扣额
    def test_pause_and_resume_across_engines(self):
        with running_source() as (base, _src):
            store = Store.load(self.workdir)
            req = self.requirement(store)
            # 第一段：只跑一轮，申请被受理后即停（模拟操作员在等待处理时暂停）
            engine1 = self.new_engine(base)
            engine1.run(interval_seconds=0.1, max_rounds=1)
            quota_mid = get_json(base + "/jobs")["quota_used"]
            self.assertEqual(quota_mid, 1)
            mid = Store.load(self.workdir).requirements[req.key]
            self.assertTrue(mid.apply_attempted)
            self.assertIsNotNone(mid.application_id)

            # 请求暂停：再启动一个引擎应在当前需求处理完后安全停下，不新建申请
            engine_paused = self.new_engine(base)
            engine_paused.request_pause()
            engine_paused.run(interval_seconds=0.1, max_rounds=2)
            self.assertEqual(get_json(base + "/jobs")["quota_used"], 1)

            # 模拟新进程从同一工作目录接手
            engine2 = self.new_engine(base)
            admin(base, seconds=10)
            engine2.run(interval_seconds=0.1, max_rounds=8)
            store = Store.load(self.workdir)
            req = store.requirements[req.key]
            self.assertEqual(req.state, COLLECTED)
            self.assertEqual(get_json(base + "/jobs")["quota_used"], quota_mid, "接手后不得重复扣额")

    # 9) 状态持久化无需浏览器即可读回，且写入是原子文件
    def test_store_persistence_roundtrip(self):
        store = Store.load(self.workdir)
        req = self.requirement(store)
        req.state = CREATE_PENDING
        req.apply_attempted = True
        store.save()
        self.assertTrue((self.workdir / "state.json").exists())
        self.assertFalse((self.workdir / "state.json.tmp").exists())

        again = Store.load(self.workdir)
        loaded = again.requirements[req.key]
        self.assertEqual(loaded.state, CREATE_PENDING)
        self.assertTrue(loaded.apply_attempted)
        self.assertEqual((loaded.code, loaded.year, loaded.scope), ("C02", "2025", "单体"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
