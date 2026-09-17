from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import re
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from thread_knowledge.models import Attachment, RawMessage, RawThread

_EPOCH_MS_RE = re.compile(r"(?<!\d)(1\d{12})(?!\d)")


class TeamsWebConnector:
    """Playwright adapter for a Microsoft Teams channel visible to the current user.

    This is a browser fallback for environments where Microsoft Graph application
    access is not available. It reads only content Teams has rendered for the signed-in
    user and therefore intentionally avoids private/undocumented Teams HTTP APIs.

    The connector uses a persistent Chromium profile so interactive login/MFA can be
    completed once and reused on later runs.

    Notes:
      * Teams virtualizes channel history, so ``iter_thread_ids`` scrolls through the
        channel and snapshots threads while they are mounted in the DOM.
      * Selectors are centralized in this class because Teams Web changes over time.
      * Headless mode is best used only after the persistent profile is authenticated.
    """

    ROOT_SELECTOR = '[data-tid="channel-pane-message"]'
    REPLY_PANE_SELECTOR = '[data-tid="channel-replies-runway"]'
    REPLY_SELECTOR = ','.join(
        [
            '[data-tid="channel-replies-pane-message"]',
            '[data-tid="response-surface"]',
        ]
    )
    EXPAND_SELECTOR = 'button[data-tid="response-summary-button"]'

    BODY_SELECTORS = (
        '[id^="message-body"]',
        '[data-testid="message-body-flex-wrapper"]',
        '.fui-ChatMessage__body',
    )
    AUTHOR_SELECTORS = (
        '[data-tid="message-author-name"]',
        '[data-tid="author"]',
        '[data-tid="message-author"]',
    )
    TIME_SELECTORS = (
        'time[datetime]',
        '[data-tid="message-timestamp"]',
        '[data-tid="timestamp"]',
    )

    def __init__(
        self,
        *,
        channel_url: str,
        profile_dir: str | Path = ".teams-web-profile",
        attachment_dir: str | Path = "data/teams-web-attachments",
        headless: bool = False,
        navigation_timeout_ms: int = 120_000,
        settle_seconds: float = 1.0,
        max_idle_scrolls: int = 4,
    ) -> None:
        self.channel_url = channel_url
        self.profile_dir = Path(profile_dir)
        self.attachment_dir = Path(attachment_dir)
        self.headless = headless
        self.navigation_timeout_ms = navigation_timeout_ms
        self.settle_seconds = settle_seconds
        self.max_idle_scrolls = max_idle_scrolls

        self._playwright: Any | None = None
        self._context: Any | None = None
        self._page: Any | None = None
        self._threads: dict[str, RawThread] = {}
        self._started = False

    async def __aenter__(self) -> "TeamsWebConnector":
        await self.start()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def start(self) -> None:
        if self._started:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "Playwright is not installed. Install the 'teams-web' extra and run "
                "'playwright install chromium'."
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.attachment_dir.mkdir(parents=True, exist_ok=True)

        self._playwright = await async_playwright().start()
        self._context = await self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            headless=self.headless,
            viewport={"width": 1600, "height": 1000},
        )
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        self._page.set_default_timeout(30_000)
        await self._page.goto(
            self.channel_url,
            wait_until="domcontentloaded",
            timeout=self.navigation_timeout_ms,
        )
        self._started = True

    async def aclose(self) -> None:
        if self._context is not None:
            await self._context.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._context = None
        self._playwright = None
        self._page = None
        self._started = False

    async def wait_until_ready(self, timeout_ms: int | None = None) -> None:
        """Wait until a channel message is rendered.

        On the first run keep ``headless=False`` and complete normal Teams login/MFA in
        the opened Chromium window. The authenticated profile is reused afterwards.
        """
        await self.start()
        assert self._page is not None
        await self._page.locator(self.ROOT_SELECTOR).first.wait_for(
            state="visible",
            timeout=timeout_ms or self.navigation_timeout_ms,
        )

    async def iter_thread_ids(self, *, limit: int | None = None) -> AsyncIterator[str]:
        await self.wait_until_ready()
        assert self._page is not None

        seen: set[str] = set()
        idle_scrolls = 0

        while True:
            roots = self._page.locator(self.ROOT_SELECTOR)
            count = await roots.count()
            discovered_this_pass = 0

            for index in range(count):
                root = roots.nth(index)
                if not await root.is_visible():
                    continue
                thread_id = await self._element_id(root, prefix="thread")
                if thread_id in seen:
                    continue

                thread = await self._snapshot_thread(root, thread_id)
                self._threads[thread_id] = thread
                seen.add(thread_id)
                discovered_this_pass += 1
                yield thread_id

                if limit is not None and len(seen) >= limit:
                    return

            loaded_more = await self._scroll_to_older()
            if discovered_this_pass == 0 and not loaded_more:
                idle_scrolls += 1
            else:
                idle_scrolls = 0

            if idle_scrolls >= self.max_idle_scrolls:
                return

    async def load_thread(self, thread_id: str) -> RawThread:
        cached = self._threads.get(thread_id)
        if cached is not None:
            return cached

        await self.wait_until_ready()
        assert self._page is not None
        roots = self._page.locator(self.ROOT_SELECTOR)
        count = await roots.count()
        for index in range(count):
            root = roots.nth(index)
            candidate = await self._element_id(root, prefix="thread")
            if candidate == thread_id:
                thread = await self._snapshot_thread(root, thread_id)
                self._threads[thread_id] = thread
                return thread
        raise KeyError(
            f"Thread {thread_id!r} is not currently rendered and was not cached. "
            "Call iter_thread_ids() before load_thread() when using Teams Web."
        )

    async def _snapshot_thread(self, root: Any, thread_id: str) -> RawThread:
        root_message = await self._extract_message(root, thread_id=thread_id, parent_id=None)
        messages = [root_message]

        expand = root.locator(self.EXPAND_SELECTOR).first
        if await expand.count() and await expand.is_visible():
            try:
                await expand.click(timeout=5_000)
                assert self._page is not None
                pane = self._page.locator(self.REPLY_PANE_SELECTOR).last
                await pane.wait_for(state="visible", timeout=10_000)
                await asyncio.sleep(self.settle_seconds)

                replies = pane.locator(self.REPLY_SELECTOR)
                reply_count = await replies.count()
                seen_reply_ids: set[str] = set()
                for idx in range(reply_count):
                    reply = replies.nth(idx)
                    if not await reply.is_visible():
                        continue
                    reply_id = await self._element_id(reply, prefix="reply")
                    if reply_id in seen_reply_ids or reply_id == root_message.external_id:
                        continue
                    seen_reply_ids.add(reply_id)
                    messages.append(
                        await self._extract_message(
                            reply,
                            thread_id=thread_id,
                            parent_id=root_message.external_id,
                        )
                    )
            except Exception:
                # A thread without successfully expanded replies is still useful and can
                # be re-indexed on a later run. DOM changes should not abort the channel.
                pass
            finally:
                await self._close_reply_pane()

        return RawThread(external_id=thread_id, messages=tuple(messages), source="teams-web")

    async def _extract_message(
        self,
        element: Any,
        *,
        thread_id: str,
        parent_id: str | None,
    ) -> RawMessage:
        message_id = await self._element_id(element, prefix="message")
        text = await self._first_text(element, self.BODY_SELECTORS)
        if not text:
            text = (await element.inner_text()).strip()
        author = await self._first_text(element, self.AUTHOR_SELECTORS) or None
        created_at = await self._extract_timestamp(element, message_id)
        source_url = self._message_source_url(message_id)
        attachments = await self._extract_attachments(element, message_id)

        return RawMessage(
            external_id=message_id,
            thread_id=thread_id,
            parent_id=parent_id,
            author=author,
            created_at=created_at,
            text=text,
            source_url=source_url,
            attachments=tuple(attachments),
        )

    async def _extract_attachments(self, element: Any, message_id: str) -> list[Attachment]:
        attachments: list[Attachment] = []
        images = element.locator('img[src]')
        count = await images.count()
        for idx in range(count):
            image = images.nth(idx)
            src = await image.get_attribute("src")
            if not src or src.startswith("data:image/svg"):
                continue
            alt = (await image.get_attribute("alt")) or ""
            # Avatars are usually small and carry author/profile semantics. Keep likely
            # content images only; exact dimensions may not exist until rendered.
            box = await image.bounding_box()
            if box and box.get("width", 0) <= 64 and box.get("height", 0) <= 64:
                continue

            attachment_id = f"{message_id}:image:{idx}"
            suffix = self._image_suffix(src)
            local_path = self.attachment_dir / f"{self._safe_name(attachment_id)}{suffix}"
            try:
                await image.screenshot(path=str(local_path))
            except Exception:
                continue

            media_type, _ = mimetypes.guess_type(local_path.name)
            attachments.append(
                Attachment(
                    kind="image",
                    url=src,
                    name=alt or local_path.name,
                    media_type=media_type or "image/png",
                    local_path=str(local_path),
                    external_id=attachment_id,
                )
            )
        return attachments

    async def _scroll_to_older(self) -> bool:
        assert self._page is not None
        before = await self._visible_root_fingerprints()
        result = await self._page.evaluate(
            """
            (rootSelector) => {
              const root = document.querySelector(rootSelector);
              if (!root) return false;
              let el = root.parentElement;
              while (el && el !== document.body) {
                const style = getComputedStyle(el);
                if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                    el.scrollHeight > el.clientHeight) {
                  const old = el.scrollTop;
                  el.scrollTop = 0;
                  el.dispatchEvent(new Event('scroll', {bubbles: true}));
                  return old !== el.scrollTop || el.scrollTop === 0;
                }
                el = el.parentElement;
              }
              return false;
            }
            """,
            self.ROOT_SELECTOR,
        )
        await asyncio.sleep(self.settle_seconds)
        after = await self._visible_root_fingerprints()
        return bool(result) and before != after

    async def _visible_root_fingerprints(self) -> tuple[str, ...]:
        assert self._page is not None
        roots = self._page.locator(self.ROOT_SELECTOR)
        values: list[str] = []
        for idx in range(await roots.count()):
            root = roots.nth(idx)
            if await root.is_visible():
                values.append(await self._element_id(root, prefix="thread"))
        return tuple(values)

    async def _close_reply_pane(self) -> None:
        assert self._page is not None
        candidates = (
            'button[aria-label="Close"]',
            'button[data-tid="close-button"]',
            'button[data-tid="channel-replies-pane-close-button"]',
        )
        for selector in candidates:
            button = self._page.locator(self.REPLY_PANE_SELECTOR).locator(selector).first
            try:
                if await button.count() and await button.is_visible():
                    await button.click(timeout=3_000)
                    await asyncio.sleep(0.2)
                    return
            except Exception:
                continue

    async def _element_id(self, element: Any, *, prefix: str) -> str:
        attrs = await element.evaluate(
            """
            el => {
              const result = {};
              for (const name of el.getAttributeNames()) result[name] = el.getAttribute(name);
              let cur = el;
              for (let i = 0; cur && i < 4; i++, cur = cur.parentElement) {
                for (const name of cur.getAttributeNames()) {
                  if (!(name in result)) result[name] = cur.getAttribute(name);
                }
              }
              return result;
            }
            """
        )
        preferred = (
            "data-message-id",
            "data-mid",
            "data-id",
            "id",
            "data-tid",
        )
        for name in preferred:
            value = attrs.get(name)
            if value:
                epoch = _EPOCH_MS_RE.search(value)
                if epoch:
                    return epoch.group(1)
                if name != "data-tid" or value not in {
                    "channel-pane-message",
                    "channel-replies-pane-message",
                    "response-surface",
                }:
                    return str(value)

        raw = (await element.inner_text()).strip()
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]
        return f"{prefix}:{digest}"

    async def _extract_timestamp(self, element: Any, element_id: str) -> datetime:
        for selector in self.TIME_SELECTORS:
            loc = element.locator(selector).first
            if not await loc.count():
                continue
            value = await loc.get_attribute("datetime")
            if value:
                parsed = self._parse_datetime(value)
                if parsed is not None:
                    return parsed
            title = await loc.get_attribute("title")
            if title:
                parsed = self._parse_datetime(title)
                if parsed is not None:
                    return parsed

        match = _EPOCH_MS_RE.search(element_id)
        if match:
            return datetime.fromtimestamp(int(match.group(1)) / 1000, tz=timezone.utc)

        # Timestamp is mandatory in the common model. Keep it explicit that this value
        # is capture time rather than silently inventing source chronology.
        return datetime.now(timezone.utc)

    async def _first_text(self, element: Any, selectors: tuple[str, ...]) -> str:
        for selector in selectors:
            loc = element.locator(selector).first
            if await loc.count():
                try:
                    text = (await loc.inner_text()).strip()
                    if text:
                        return text
                except Exception:
                    continue
        return ""

    def _message_source_url(self, message_id: str) -> str:
        # The channel URL is always valid provenance. Teams DOM message IDs are not
        # consistently addressable as URL fragments, so don't fabricate deep links.
        return self.channel_url

    @staticmethod
    def _parse_datetime(value: str) -> datetime | None:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except ValueError:
            return None

    @staticmethod
    def _image_suffix(url: str) -> str:
        path = urlparse(url).path.lower()
        for suffix in (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"):
            if path.endswith(suffix):
                return suffix
        return ".png"

    @staticmethod
    def _safe_name(value: str) -> str:
        return re.sub(r"[^a-zA-Z0-9._-]+", "_", value)
