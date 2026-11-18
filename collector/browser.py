"""基于真实 Chromium 的门户会话。

采集端只通过可见页面控件与页面触发的下载取得资料：
不直接调用 /apply、/jobs、/ticket、/download 等页面内部请求，
这些请求仅作为“页面已经做了什么”的网络证据被记录。
/__admin/* 演练控制入口一律不碰。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

# 页面控件与门户文案（与 data/portal.html 保持一致的可见锚点）
SEL_COMPANY = "#company"
SEL_YEAR = "#year"
SEL_TITLE = "#title"
SEL_CONTEXT = "#context"
SEL_APPLY = "#apply"
SEL_REFRESH = "#refresh"
SEL_MESSAGE = "#message"
SEL_JOB_CARDS = "#jobs article"

UNCERTAIN_MESSAGE = "申请结果尚未确认"
ACCEPTED_PREFIX = "已受理 "

# 申请记录卡片文案形如：申请 E-1 企业 C02 年度 2025 范围 单体 状态 ready 办理 automatic
# 卡片内还含按钮/链接文字，actor 用非贪婪匹配并在“状态…办理”锚点内截断
CARD_RE = re.compile(
    r"申请\s+(?P<id>\S+)\s+企业\s+(?P<company>\S+)\s+年度\s+(?P<year>\S+)\s+"
    r"范围\s+(?P<scope>\S+)\s+状态\s+(?P<state>queued|ready)\s+办理\s+(?P<actor>automatic|operator)"
)
CONTEXT_RE = re.compile(r"企业代码\s+(?P<company>\S+?)；报告期\s+(?P<year>\S+?)；披露范围\s+(?P<scope>\S+)")
APPLY_ID_RE = re.compile(r"[E]-[\w-]+")


class PortalError(RuntimeError):
    """门户页面行为不符合预期，需保留现场并交操作员判断。"""


class DownloadFailed(PortalError):
    """下载未成功触发或被门户拒绝（过期、暂不可用等），可从同一申请重新领取地址。"""


class PortalSession:
    """一个真实浏览器会话；cookie 随 context 保持，下载地址与会话绑定。"""

    def __init__(self, headless: bool = True, default_timeout_ms: int = 10_000):
        self.headless = headless
        self.default_timeout_ms = default_timeout_ms
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        # 页面实际发出的产品请求证据（不含 /__admin 与静态页面本身的控制判断）
        self.responses: list[dict] = []
        self.failed_requests: list[dict] = []

    # ---- 生命周期 ----
    def open(self, base_url: str) -> None:
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=self.headless, args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        self._context = self._browser.new_context(accept_downloads=True)
        self._context.set_default_timeout(self.default_timeout_ms)
        self.page = self._context.new_page()
        self.page.on("response", self._on_response)
        self.page.on("requestfailed", self._on_request_failed)
        self.page.goto(base_url, wait_until="domcontentloaded")
        self.page.wait_for_selector(SEL_CONTEXT)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()
            self._pw = self._browser = self._context = self.page = None

    def __enter__(self) -> "PortalSession":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- 网络证据（只记录，不主动请求）----
    def _on_response(self, response) -> None:
        url = response.url
        if "/__admin/" in url or url.endswith("/health"):
            return
        try:
            status = response.status
        except Exception:
            status = -1
        self.responses.append({"url": url, "status": status})

    def _on_request_failed(self, request) -> None:
        url = request.url
        if "/__admin/" in url or url.endswith("/health"):
            return
        self.failed_requests.append({"url": url, "failure": request.failure})

    def statuses_for(self, path_fragment: str) -> list[int]:
        return [r["status"] for r in self.responses if path_fragment in r["url"]]

    def count_for(self, path_fragment: str) -> int:
        return sum(1 for r in self.responses if path_fragment in r["url"])

    def failed_for(self, path_fragment: str) -> list[dict]:
        return [r for r in self.failed_requests if path_fragment in r["url"]]

    # ---- 证据留存 ----
    def save_evidence(self, html_path: Path, screenshot_path: Path) -> None:
        """保存来源页面 DOM 与截图，作为该结论“由门户确认”的依据。"""
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(self.page.content(), encoding="utf-8")
        try:
            self.page.screenshot(path=str(screenshot_path), full_page=True)
        except PlaywrightTimeoutError:
            # 截图失败不应影响采集本身；DOM 快照仍是有效来源依据
            pass

    def _wait_context_settled(self, min_new_responses: int) -> None:
        """等到至少新增 min_new_responses 个 /context 响应，且卡片已就绪。"""
        baseline = len(self.statuses_for("/context"))
        self._poll_until(
            lambda: len(self.statuses_for("/context")) - baseline >= min_new_responses
            and self.page.eval_on_selector(SEL_CONTEXT, "el => el.dataset.ready") == "yes"
        )

    def _poll_until(self, predicate) -> None:
        import time

        deadline = time.monotonic() + self.default_timeout_ms / 1000
        while time.monotonic() < deadline:
            if predicate():
                return
            self.page.wait_for_timeout(50)
        raise PortalError("等待门户页面状态变化超时")

    # ---- 步骤 1：选择企业与年度，读取上下文卡片 ----
    def read_context_card(self, company: str, year: str) -> dict:
        """选择企业/年度并等待上下文卡片确认。

        标题变化只是选择结果，不能作为确认；必须等 data-ready=yes
        并从卡片文案解析企业代码、报告期、披露范围三项。
        页面选择后有约 130ms 异步刷新，且企业、年度两个选择器各触发一次
        /context，data-ready 可能残留上一次的 yes，因此必须等本轮两次
        响应都返回并核对字段，避免把上一家企业的确认张冠李戴。
        """
        self.page.select_option(SEL_COMPANY, company)
        self.page.select_option(SEL_YEAR, str(year))
        self._wait_context_settled(min_new_responses=2)
        title = self.page.text_content(SEL_TITLE) or ""
        card_text = self.page.text_content(SEL_CONTEXT) or ""
        match = CONTEXT_RE.search(card_text)
        if not match:
            raise PortalError(f"上下文卡片已就绪但无法解析确认字段：{card_text!r}")
        return {
            "company": match.group("company"),
            "year": match.group("year"),
            "scope": match.group("scope"),
            "title_text": title.strip(),
            "card_text": card_text.strip(),
        }

    def apply_button_enabled(self) -> bool:
        return self.page.is_enabled(SEL_APPLY)

    # ---- 步骤 2：提交申请（无幂等键，结果可能不确定）----
    def submit_application(self) -> dict:
        """点击可见的“申请导出”。

        返回：
        - {'outcome': 'accepted', ...}：页面明确显示已受理；
        - {'outcome': 'rejected', ...}：门户明确拒绝（如 409，额度未扣）；
        - {'outcome': 'uncertain', ...}：连接中断等导致受理结果无法确定。
        uncertain 绝不意味着可以再点一次——恢复时以申请记录为准。
        """
        before_resp = len(self.responses)
        before_fail = len(self.failed_requests)
        self.page.click(SEL_APPLY)

        def _settled() -> bool:
            message = (self.page.text_content(SEL_MESSAGE) or "").strip()
            if message:
                return True
            return len(self.failed_requests) > before_fail or any(
                "/apply" in r["url"] for r in self.responses[before_resp:]
            )

        self._poll_until(_settled)

        message = (self.page.text_content(SEL_MESSAGE) or "").strip()
        apply_statuses = [
            r["status"] for r in self.responses[before_resp:] if "/apply" in r["url"]
        ]
        apply_failures = [
            r for r in self.failed_requests[before_fail:] if "/apply" in r["url"]
        ]
        # 409“已有导出正在办理”不能直接当作未受理：断连后浏览器会自动重试，
        # 第一次请求可能已经创建申请并扣额，重试才撞上 409。一律按“结果不确定”
        # 处理，下一轮以申请记录核实，绝不再点。
        hard_reject = [s for s in apply_statuses if s > 0 and s >= 400 and s != 409]
        if hard_reject and 409 not in apply_statuses:
            return {
                "outcome": "rejected",
                "reason": message or f"门户拒绝受理，/apply 响应状态 {apply_statuses}",
                "apply_statuses": apply_statuses,
            }
        if message.startswith(ACCEPTED_PREFIX) and not apply_failures and 409 not in apply_statuses:
            found = APPLY_ID_RE.search(message)
            return {
                "outcome": "accepted",
                "application_id": found.group(0) if found else None,
                "message": message,
                "apply_statuses": apply_statuses,
            }
        # 断连（请求失败无响应）、重试撞上 409，或任何无法确认受理结果的情形
        if apply_failures:
            reason = f"申请请求连接中断：{apply_failures[0].get('failure')}，受理结果无法确定"
        elif 409 in apply_statuses:
            reason = "响应显示已有导出正在办理（可能是断连后的重试），本次创建结果无法确定"
        else:
            reason = message or f"未见受理确认，/apply 响应状态 {apply_statuses or '无'}"
        return {
            "outcome": "uncertain",
            "reason": reason,
            "apply_statuses": apply_statuses,
        }

    # ---- 步骤 3：刷新并读取申请记录（可见卡片）----
    def refresh_jobs(self) -> list[dict]:
        # 以页面确实完成一次 /jobs 往返为准（计数器仅用于等待，数据只从 DOM 读）。
        # 断连等情况下请求失败也要解除等待，交由上层按无记录/不确定处理。
        before_resp = self.count_for("/jobs")
        before_fail = len(self.failed_requests)
        self.page.click(SEL_REFRESH)
        self._poll_until(
            lambda: self.count_for("/jobs") > before_resp
            or len(self.failed_requests) > before_fail
        )
        self.page.wait_for_timeout(80)  # 让卡片渲染完成后再读文案
        cards = []
        count = self.page.locator(SEL_JOB_CARDS).count()
        for i in range(count):
            card = self.page.locator(SEL_JOB_CARDS).nth(i)
            text = (card.text_content() or "").strip()
            match = CARD_RE.search(text)
            if not match:
                continue
            cards.append(
                {
                    "id": match.group("id"),
                    "company": match.group("company"),
                    "year": match.group("year"),
                    "scope": match.group("scope"),
                    "state": match.group("state"),
                    "actor": match.group("actor"),
                    "text": text,
                    "selector": f'{SEL_JOB_CARDS}:nth-child({i + 1})',
                }
            )
        return cards

    # ---- 步骤 4：从某张申请卡片领取短时有效的下载地址 ----
    def claim_tickets(self, application_id: str) -> list[dict]:
        card = self._card_locator(application_id)
        ticket_button = card.locator(f'button[data-ticket="{application_id}"]')
        ticket_button.wait_for(state="visible", timeout=self.default_timeout_ms)
        if ticket_button.is_disabled():
            raise PortalError(f"申请 {application_id} 尚未就绪，无法领取下载地址")
        ticket_button.click()
        # 等待页面渲染 a[download]
        self.page.wait_for_selector(
            f'#jobs article[data-application="{application_id}"] a[download]',
            timeout=self.default_timeout_ms,
        )
        anchors = card.locator("a[download]")
        result = []
        for i in range(anchors.count()):
            a = anchors.nth(i)
            href = a.get_attribute("href") or ""
            expires = None
            m = re.search(r"expires=(\d+)", href)
            if m:
                expires = int(m.group(1))
            result.append(
                {
                    "part": (a.get_attribute("download") or "").replace(
                        f"{application_id}-", ""
                    ).removesuffix(".txt"),
                    "text": (a.text_content() or "").strip(),
                    "href": href,
                    "expires_hint": expires,
                }
            )
        if not result:
            raise PortalError(f"申请 {application_id} 未领取到任何下载地址")
        return result

    # ---- 步骤 5：通过页面链接触发真实下载 ----
    def download_from_ticket(self, application_id: str, href: str, dest: Path) -> None:
        """点击页面上的下载链接并保存。

        附件导航在 Chromium 中可靠触发 download 事件；若门户拒绝（410 过期 /
        503 暂不可用），浏览器停在错误响应、没有 download 事件，
        则依据页面实际收到的响应状态判定。调用方应从同一申请重新领取地址，
        而不是另开申请。
        """
        card = self._card_locator(application_id)
        anchor = card.locator(f'a[href="{href}"]')
        anchor.wait_for(state="visible", timeout=self.default_timeout_ms)
        path = href.split("?")[0]
        before_resp = self.count_for(path)
        before_fail = len(self.failed_requests)
        holder: dict = {}

        def _on_download(download):
            holder["download"] = download

        self.page.on("download", _on_download)
        try:
            anchor.click()
            self._poll_until(
                lambda: "download" in holder
                or self.count_for(path) > before_resp
                or len(self.failed_requests) > before_fail
            )
            if "download" not in holder:
                statuses = self.statuses_for(path)
                if self.failed_for(path):
                    reason = f"下载请求失败：{self.failed_for(path)[-1].get('failure')}"
                else:
                    reason = f"门户拒绝下载（最近 HTTP 状态 {statuses[-1:] or '无'}）"
                raise DownloadFailed(
                    f"{reason}；地址可能已过期或该册暂不可用，应从同一申请重新领取地址"
                )
            download = holder["download"]
            # 门户返回错误状态时浏览器也可能先建 download 再取消
            canceled = download.failure()
            if canceled:
                statuses = self.statuses_for(path)
                raise DownloadFailed(
                    f"浏览器下载被取消（{canceled}，最近 HTTP 状态 {statuses[-1:] or '无'}）；"
                    "应从同一申请重新领取地址"
                )
        finally:
            self.page.remove_listener("download", _on_download)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        try:
            download.save_as(str(tmp))
        except PlaywrightTimeoutError as exc:
            raise DownloadFailed(f"原件保存失败：{exc}；应从同一申请重新领取地址") from exc
        tmp.replace(dest)

    def _card_locator(self, application_id: str):
        selector = f'{SEL_JOB_CARDS}[data-application="{application_id}"]'
        locator = self.page.locator(selector)
        if locator.count() == 0:
            raise PortalError(f"申请记录中找不到 {application_id}，请先刷新申请记录")
        return locator.first
