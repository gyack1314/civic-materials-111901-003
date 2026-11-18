"""采集状态机：把选择、等待、下载、交接接起来，并保证不重复扣额。

核心纪律：
1. 只有上下文卡片确认的企业代码/报告期/披露范围与申请完全一致才允许提交；
2. 提交结果不确定时停留 CREATE_PENDING，恢复后先刷新申请记录，绝不二次提交；
3. 按 (企业代码, 报告期, 披露范围) 三元组在申请记录中确认归属，
   绝不按“最新一条”认领；actor=operator 的手工申请保留给操作员；
4. 只下载门户在该申请卡片上给出的册别，过期就从同一申请重新领取地址；
5. 仅当门户显示的文件集合与已保存原件逐一相符才报告 COLLECTED。
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import handover
from .browser import DownloadFailed, PortalError, PortalSession
from .model import (
    BLOCKED,
    CLAIMED,
    COLLECTED,
    CONTEXT_MISMATCH,
    CREATE_PENDING,
    PARTIAL,
    PENDING_VERIFICATION,
    RECORDED,
    SavedFile,
    Store,
)

# 门户对“仍在处理”的领取会返回 409；补充册暂不可用为 503；地址过期为 410
STILL_PROCESSING = 409


@dataclass
class ReconcileResult:
    key: str
    state: str
    detail: str


class Engine:
    def __init__(
        self,
        base_url: str,
        workdir: str | Path,
        headless: bool = True,
        timeout_ms: int = 10_000,
    ):
        self.base_url = base_url.rstrip("/")
        self.store = Store.load(workdir)
        self.store.ensure_dirs()
        self.headless = headless
        self.timeout_ms = timeout_ms
        self._stop = threading.Event()

    # ---- 暂停 / 接手 ----
    def request_pause(self) -> None:
        """请求在当前需求处理完成后安全停下；状态已逐需求落盘，可随时接手。"""
        self._stop.set()

    @property
    def pause_requested(self) -> bool:
        return self._stop.is_set()

    def run(self, interval_seconds: float = 1.0, max_rounds: Optional[int] = None) -> dict:
        """循环对账直到全部收齐、被暂停或达到轮次上限。每轮结束都落盘并写交接单。"""
        rounds = 0
        last_summary = None
        with PortalSession(headless=self.headless, default_timeout_ms=self.timeout_ms) as session:
            session.open(self.base_url)
            while not self._stop.is_set():
                rounds += 1
                results = []
                for req in self.store.pending():
                    if self._stop.is_set():
                        break
                    try:
                        results.append(self._reconcile_one(session, req))
                    except PortalError as exc:
                        # 门户行为异常：保留现场与证据。已受理过的需求保持原状态下轮自愈，
                        # 只有从未推进的才标 BLOCKED 交操作员
                        previous = req.state
                        if previous not in (CLAIMED, PARTIAL, CREATE_PENDING):
                            req.state = BLOCKED
                        req.add_note(f"门户异常（状态保持 {req.state}）：{exc}")
                        self.store.save()
                        results.append(ReconcileResult(req.key, req.state, str(exc)))
                handover.write_handover(self.store)
                last_summary = handover.build_manifest(self.store)
                if not self.store.pending():
                    break
                if max_rounds is not None and rounds >= max_rounds:
                    break
                self._stop.wait(interval_seconds)
        return {
            "rounds": rounds,
            "paused": self._stop.is_set(),
            "summary": last_summary or handover.build_manifest(self.store),
        }

    # ---- 单项需求对账 ----
    def _reconcile_one(self, session: PortalSession, req) -> ReconcileResult:
        req.rounds += 1
        originals_dir, evidence_dir = self.store.req_dirs(req.key)
        tag = f"round{req.rounds}"

        # 1) 选择企业/年度，读取上下文卡片（标题变化不算确认）
        card = session.read_context_card(req.code, req.year)
        session.save_evidence(
            evidence_dir / f"context-{tag}.html",
            evidence_dir / f"context-{tag}.png",
        )
        req.add_evidence(self._rel(evidence_dir / f"context-{tag}.html"))
        req.add_evidence(self._rel(evidence_dir / f"context-{tag}.png"))

        if (card["company"], card["year"], card["scope"]) != (
            req.code,
            req.year,
            req.scope,
        ):
            # 同名企业/范围混淆的最后一道闸门：不消耗任何额度
            req.state = CONTEXT_MISMATCH
            req.add_note(
                "上下文卡片确认字段与申请不符，已阻止提交："
                f"卡片={card['company']}/{card['year']}/{card['scope']}，"
                f"申请={req.code}/{req.year}/{req.scope}"
            )
            self.store.save()
            return ReconcileResult(req.key, CONTEXT_MISMATCH, "上下文卡片与申请不符，未提交")

        # 2) 刷新申请记录，按三元组确认归属（不按最新一条）
        jobs = session.refresh_jobs()
        session.save_evidence(
            evidence_dir / f"jobs-{tag}.html", evidence_dir / f"jobs-{tag}.png"
        )
        req.add_evidence(self._rel(evidence_dir / f"jobs-{tag}.html"))
        req.add_evidence(self._rel(evidence_dir / f"jobs-{tag}.png"))

        matches = [
            j for j in jobs
            if (j["company"], j["year"], j["scope"]) == (req.code, req.year, req.scope)
        ]
        automatic = [j for j in matches if j["actor"] == "automatic"]
        manual = [j for j in matches if j["actor"] != "automatic"]

        # 看到有卡片变为就绪，说明此前“在办”的导出已结束，解除 409 在办闸门
        if any(j["state"] == "ready" for j in jobs):
            req.saw_busy_guard = False

        # 3) 没有任何受理记录时：区分“从未创建”和“创建结果不确定”
        if not matches:
            any_queued = any(j["state"] != "ready" for j in jobs)
            if req.apply_attempted and (any_queued or req.saw_busy_guard or req.rebuild_count >= 1):
                # 在办卡片未消失、409 在办闸门未解除，或已用过唯一一次证据补建：
                # 保持待核实，禁止再点，直到出现可证明在办结束的就绪卡片
                req.state = CREATE_PENDING
                if any_queued or req.saw_busy_guard:
                    reason = "门户仍有导出在办，创建结果保持待核实"
                else:
                    reason = "补建后仍无法确认，转操作员核实"
                req.add_note(f"已提交但无匹配受理记录（{reason}）；保持待核实，不再自动提交")
                self.store.save()
                return ReconcileResult(req.key, CREATE_PENDING, reason)
            if req.apply_attempted:
                # 列表已刷新：无同三元组记录、无在办卡片，且未补建过——可判定此前尝试未生效，
                # 此时补建不会重复扣额（若门户仍在办会以 409 拒绝且不产生新申请）
                req.add_note("申请记录无匹配且无在办导出，判定此前创建未生效，安全补建一次")
                req.rebuild_count += 1
                req.apply_attempted = False
            if not session.apply_button_enabled():
                req.state = BLOCKED
                req.add_note("申请按钮不可用，门户可能有其他导出正在办理")
                self.store.save()
                return ReconcileResult(req.key, BLOCKED, "申请按钮不可用")
            outcome = session.submit_application()
            if outcome["outcome"] == "rejected":
                # 门户明确拒绝（非 409 的硬拒绝）：额度未扣，保持已登记下轮再试
                req.state = RECORDED
                req.add_note(f"门户暂不受理（未扣额度）：{outcome['reason']}")
                self.store.save()
                return ReconcileResult(req.key, RECORDED, "门户暂不受理，下轮重试")
            req.apply_attempted = True
            if 409 in (outcome.get("apply_statuses") or []):
                # 409 在办是持久证据：在看到就绪卡片前不得再点
                req.saw_busy_guard = True
            if outcome["outcome"] == "accepted" and outcome.get("application_id"):
                req.application_id = outcome["application_id"]
                req.state = CLAIMED
                req.saw_busy_guard = False
                req.add_note(f"页面显示已受理，申请号 {req.application_id}（仍以申请记录为准）")
            else:
                req.state = CREATE_PENDING
                req.add_note(f"申请结果无法确定：{outcome['reason']}；不自动重试，待申请记录核实")
            self.store.save()
            return ReconcileResult(req.key, req.state, "已提交，等待申请记录/处理")

        # 4) 存在手工申请：自动化不认领，保留给操作员
        if manual and not automatic:
            req.state = PENDING_VERIFICATION
            req.add_note(
                "申请记录中存在操作员手工申请 "
                + "、".join(j["id"] for j in manual)
                + "，自动化未认领；如确属本需求，请操作员确认后录入申请号"
            )
            self.store.save()
            return ReconcileResult(
                req.key, PENDING_VERIFICATION, "存在手工申请，待操作员核实"
            )

        # 5) 自动受理记录可能有一条以上时，全部留证并待人工核实，避免重复或误认
        if len(automatic) > 1:
            req.state = PENDING_VERIFICATION
            req.add_note(
                "同一三元组出现多条自动申请记录 "
                + "、".join(j["id"] for j in automatic)
                + "，需操作员核实是否重复扣额"
            )
            self.store.save()
            return ReconcileResult(req.key, PENDING_VERIFICATION, "多条匹配记录，待核实")

        job = automatic[0]
        if req.application_id and req.application_id != job["id"]:
            # 自己记录的申请号与页面不一致，绝不静默换号
            req.state = PENDING_VERIFICATION
            req.add_note(
                f"状态中的申请号 {req.application_id} 与页面记录 {job['id']} 不一致，待人工核实"
            )
            self.store.save()
            return ReconcileResult(req.key, PENDING_VERIFICATION, "申请号不一致，待核实")
        req.application_id = job["id"]

        if job["state"] != "ready":
            # 排队卡片只表示已受理，不等于资料收齐
            req.state = CLAIMED
            req.add_note(f"申请 {job['id']} 门户状态 queued，继续等待处理")
            self.store.save()
            return ReconcileResult(req.key, CLAIMED, f"{job['id']} 排队处理中")

        # 6) 已就绪：从该申请卡片领取地址并逐册下载（地址过期则从同一申请重领）
        return self._collect_parts(session, req, job["id"], originals_dir, evidence_dir, tag)

    def _collect_parts(self, session, req, application_id, originals_dir, evidence_dir, tag):
        tickets = session.claim_tickets(application_id)
        portal_parts = {t["part"] for t in tickets}
        req.missing = {}

        for ticket in tickets:
            part = ticket["part"]
            if part in req.files:
                continue  # 已取得且校验过的原件不重复下载
            dest = originals_dir / f"{application_id}-{part}.txt"
            try:
                session.download_from_ticket(application_id, ticket["href"], dest)
            except DownloadFailed as exc:
                req.missing[part] = str(exc)
                continue
            try:
                saved = self._verify_original(dest, application_id, req)
            except ValueError as exc:
                dest.unlink(missing_ok=True)
                req.missing[part] = f"原件校验未通过：{exc}"
                continue
            req.files[part] = saved
            self.store.save()  # 逐册落盘：中断也保留已取得部分

        session.save_evidence(
            evidence_dir / f"tickets-{tag}.html", evidence_dir / f"tickets-{tag}.png"
        )
        req.add_evidence(self._rel(evidence_dir / f"tickets-{tag}.html"))
        req.add_evidence(self._rel(evidence_dir / f"tickets-{tag}.png"))

        saved_parts = set(req.files.keys())
        if portal_parts and portal_parts <= saved_parts:
            # 仅当门户显示的文件集合与已保存原件完全相符
            req.missing = {}
            req.state = COLLECTED
            detail = "门户文件集合与已保存原件相符"
        elif saved_parts:
            req.state = PARTIAL
            for part in portal_parts - saved_parts:
                req.missing.setdefault(part, "本轮未取得，可从同一申请重新领取地址后重试")
            detail = f"已取得 {sorted(saved_parts)}，缺 {sorted(req.missing)}"
        else:
            req.state = CLAIMED
            detail = "门户已就绪但本轮下载均未成功，稍后从同一申请重试"
        self.store.save()
        return ReconcileResult(req.key, req.state, detail)

    # ---- 原件校验：册内声明必须与申请号/企业/报告期/范围一致 ----
    def _verify_original(self, dest: Path, application_id: str, req) -> SavedFile:
        raw = dest.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        try:
            declaration = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError(f"原件不是可读的声明文本：{exc}") from exc
        expected = {
            "application": application_id,
            "company": req.code,
            "year": req.year,
            "scope": req.scope,
        }
        for k, v in expected.items():
            if str(declaration.get(k)) != str(v):
                raise ValueError(
                    f"原件声明 {k}={declaration.get(k)!r} 与申请值 {v!r} 不符"
                )
        part = declaration.get("part")
        if not part:
            raise ValueError("原件缺少册别声明 part")
        return SavedFile(
            part=str(part),
            filename=dest.name,
            relative_path=self._rel(dest),
            sha256=digest,
            size=len(raw),
            application_id=application_id,
            company=req.code,
            year=req.year,
            scope=req.scope,
        )

    def _rel(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.store.workdir))
        except ValueError:
            return str(path)
