from __future__ import annotations

import asyncio
import base64
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
            attachment = await self._materialize_best_image_attachment(
                image=image,
                attachment_id=attachment_id,
                fallback_name=alt,
            )
            if attachment is not None:
                attachments.append(attachment)
        return attachments


    async def _materialize_best_image_attachment(
        self,
        *,
        image: Any,
        attachment_id: str,
        fallback_name: str,
    ) -> Attachment | None:
        metadata = await self._collect_image_metadata(image)
        candidates = list(metadata["candidates"])
        viewer_metadata = await self._collect_viewer_image_metadata(image)
        if viewer_metadata is not None:
            candidates = self._merge_candidate_lists(viewer_metadata["candidates"], candidates)
            metadata = self._prefer_larger_metadata(metadata, viewer_metadata)

        downloaded = await self._download_best_candidate(candidates, attachment_id)
        if downloaded is not None:
            local_path, content_type, chosen_url = downloaded
            media_type, _ = mimetypes.guess_type(local_path.name)
            return Attachment(
                kind="image",
                url=chosen_url,
                name=fallback_name or local_path.name,
                media_type=content_type or media_type or "image/png",
                local_path=str(local_path),
                external_id=attachment_id,
            )

        # Last resort: capture the best rendered image available. Prefer the image seen
        # in the full-screen viewer if it was opened successfully.
        target = image
        chosen_url = metadata.get("current_src") or metadata.get("src")
        if viewer_metadata is not None and viewer_metadata.get("locator") is not None:
            target = viewer_metadata["locator"]
            chosen_url = viewer_metadata.get("current_src") or viewer_metadata.get("src") or chosen_url

        suffix = self._image_suffix(chosen_url or "")
        local_path = self.attachment_dir / f"{self._safe_name(attachment_id)}{suffix}"
        try:
            await target.screenshot(path=str(local_path))
        except Exception:
            return None

        media_type, _ = mimetypes.guess_type(local_path.name)
        return Attachment(
            kind="image",
            url=chosen_url,
            name=fallback_name or local_path.name,
            media_type=media_type or "image/png",
            local_path=str(local_path),
            external_id=attachment_id,
        )

    async def _collect_image_metadata(self, image: Any) -> dict[str, Any]:
        data = await image.evaluate(
            """
            el => {
              const attrs = {};
              for (const name of el.getAttributeNames()) attrs[name] = el.getAttribute(name);
              const parseSrcset = (value) => {
                if (!value) return [];
                return value
                  .split(',')
                  .map(part => part.trim())
                  .filter(Boolean)
                  .map(part => {
                    const [url, descriptor] = part.split(/\\s+/, 2);
                    let width = 0;
                    if (descriptor && descriptor.endsWith('w')) width = parseInt(descriptor.slice(0, -1), 10) || 0;
                    return {url, width};
                  });
              };
              const candidates = [];
              const add = (url, width = 0, source = 'unknown') => {
                if (!url || typeof url !== 'string') return;
                if (url.startsWith('data:image/svg')) return;
                candidates.push({url, width, source});
              };

              add(el.currentSrc || '', el.naturalWidth || 0, 'currentSrc');
              add(el.src || '', el.naturalWidth || 0, 'src');
              for (const item of parseSrcset(el.srcset || '')) add(item.url, item.width, 'srcset');

              for (const [name, value] of Object.entries(attrs)) {
                if (!value) continue;
                if (name === 'src' || name === 'srcset') continue;
                const lower = name.toLowerCase();
                if (lower.includes('srcset')) {
                  for (const item of parseSrcset(value)) add(item.url, item.width, lower);
                } else if (lower.includes('src') || lower.includes('url') || lower.includes('full')) {
                  add(String(value), 0, lower);
                }
              }

              return {
                src: el.src || null,
                current_src: el.currentSrc || null,
                natural_width: el.naturalWidth || 0,
                natural_height: el.naturalHeight || 0,
                candidates,
              };
            }
            """
        )
        data["candidates"] = self._dedupe_candidates(data.get("candidates") or [])
        return data

    async def _collect_viewer_image_metadata(self, image: Any) -> dict[str, Any] | None:
        assert self._page is not None
        dialog_selectors = (
            '[role="dialog"] img[src]',
            '[aria-modal="true"] img[src]',
            '[data-tid="image-content"] img[src]',
            '[data-tid="media-viewer"] img[src]',
        )
        try:
            await image.click(button='left', timeout=3_000)
            await asyncio.sleep(0.8)
        except Exception:
            return None

        loc = None
        for selector in dialog_selectors:
            candidate = self._page.locator(selector)
            try:
                count = await candidate.count()
            except Exception:
                continue
            if not count:
                continue
            for idx in range(count - 1, -1, -1):
                item = candidate.nth(idx)
                try:
                    if await item.is_visible():
                        loc = item
                        break
                except Exception:
                    continue
            if loc is not None:
                break

        if loc is None:
            await self._dismiss_image_viewer()
            return None

        try:
            metadata = await self._collect_image_metadata(loc)
            metadata["locator"] = loc
            return metadata
        except Exception:
            return None
        finally:
            await self._dismiss_image_viewer()

    async def _dismiss_image_viewer(self) -> None:
        assert self._page is not None
        try:
            await self._page.keyboard.press('Escape')
            await asyncio.sleep(0.2)
        except Exception:
            pass

    async def _download_best_candidate(
        self,
        candidates: list[dict[str, Any]],
        attachment_id: str,
    ) -> tuple[Path, str | None, str] | None:
        for candidate in candidates:
            url = candidate.get("url")
            if not url:
                continue
            response = await self._fetch_url_bytes(url)
            if response is None:
                continue
            suffix = self._image_suffix(url)
            local_path = self.attachment_dir / f"{self._safe_name(attachment_id)}{suffix}"
            local_path.write_bytes(response["content"])
            return local_path, response.get("content_type"), url
        return None

    async def _fetch_url_bytes(self, url: str) -> dict[str, Any] | None:
        assert self._page is not None
        try:
            payload = await self._page.evaluate(
                """
                async (url) => {
                  try {
                    const response = await fetch(url, {credentials: 'include'});
                    if (!response.ok) {
                      return {ok: false, status: response.status, reason: `HTTP ${response.status}`};
                    }
                    const blob = await response.blob();
                    const dataUrl = await new Promise((resolve, reject) => {
                      const reader = new FileReader();
                      reader.onerror = () => reject(new Error('file-reader-error'));
                      reader.onload = () => resolve(String(reader.result));
                      reader.readAsDataURL(blob);
                    });
                    return {
                      ok: true,
                      content_type: blob.type || response.headers.get('content-type') || null,
                      data_url: dataUrl,
                    };
                  } catch (error) {
                    return {ok: false, reason: String(error)};
                  }
                }
                """,
                url,
            )
        except Exception:
            return None
        if not payload or not payload.get("ok"):
            return None
        data_url = payload.get("data_url") or ""
        try:
            _, encoded = data_url.split(',', 1)
            content = base64.b64decode(encoded)
        except Exception:
            return None
        return {"content": content, "content_type": payload.get("content_type")}

    @staticmethod
    def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: dict[str, dict[str, Any]] = {}
        for item in candidates:
            url = item.get("url")
            if not url or url.startswith('data:image/svg'):
                continue
            if url.startswith('data:'):
                continue
            current = seen.get(url)
            if current is None or int(item.get("width") or 0) > int(current.get("width") or 0):
                seen[url] = {
                    "url": url,
                    "width": int(item.get("width") or 0),
                    "source": item.get("source") or "unknown",
                }
        return sorted(seen.values(), key=lambda value: value.get("width", 0), reverse=True)

    @staticmethod
    def _merge_candidate_lists(*lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        for items in lists:
            merged.extend(items)
        return TeamsWebConnector._dedupe_candidates(merged)

    @staticmethod
    def _prefer_larger_metadata(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
        primary_area = int(primary.get("natural_width") or 0) * int(primary.get("natural_height") or 0)
        secondary_area = int(secondary.get("natural_width") or 0) * int(secondary.get("natural_height") or 0)
        return secondary if secondary_area > primary_area else primary

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
