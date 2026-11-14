"""Offline tests for same-user duplicate in-flight text-search suppression."""

import asyncio
import unittest
from types import SimpleNamespace

from handlers.search import _deduplicate_inflight_search


class FakeMessage:
    def __init__(self, text: str) -> None:
        self.text = text
        self.replies: list[str] = []

    async def reply_text(self, text: str) -> None:
        self.replies.append(text)


def update(text: str):
    message = FakeMessage(text)
    return SimpleNamespace(effective_message=message), message


class InflightSearchDedupTests(unittest.TestCase):
    def test_identical_normalized_text_is_skipped_until_first_finishes(self) -> None:
        async def run():
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def handler(_update, _context):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return "completed"

            wrapped = _deduplicate_inflight_search(handler)
            context = SimpleNamespace(user_data={})
            first_update, first_message = update("Find   USB hub")
            duplicate_update, duplicate_message = update("  find usb HUB  ")

            first = asyncio.create_task(wrapped(first_update, context))
            await started.wait()
            duplicate_result = await wrapped(duplicate_update, context)
            release.set()
            first_result = await first
            return calls, duplicate_result, first_result, duplicate_message, context.user_data

        calls, duplicate_result, first_result, duplicate_message, user_data = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertIsNone(duplicate_result)
        self.assertEqual(first_result, "completed")
        self.assertIn("уже обрабатывается", duplicate_message.replies[0])
        self.assertNotIn("_inflight_text_search_fingerprints", user_data)

    def test_different_queries_from_same_user_are_not_blocked(self) -> None:
        async def run():
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0

            async def handler(_update, _context):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()

            wrapped = _deduplicate_inflight_search(handler)
            context = SimpleNamespace(user_data={})
            first_update, _ = update("Find a USB hub")
            other_update, _ = update("Find a desk lamp")
            first = asyncio.create_task(wrapped(first_update, context))
            await started.wait()
            second = asyncio.create_task(wrapped(other_update, context))
            await asyncio.sleep(0)
            release.set()
            await asyncio.gather(first, second)
            return calls

        self.assertEqual(asyncio.run(run()), 2)

    def test_failed_handler_releases_fingerprint_for_retry(self) -> None:
        async def run():
            calls = 0

            async def handler(_update, _context):
                nonlocal calls
                calls += 1
                if calls == 1:
                    raise RuntimeError("expected test failure")
                return "retry completed"

            wrapped = _deduplicate_inflight_search(handler)
            context = SimpleNamespace(user_data={})
            request, _ = update("Find a camera")
            with self.assertRaises(RuntimeError):
                await wrapped(request, context)
            return await wrapped(request, context), calls

        result, calls = asyncio.run(run())
        self.assertEqual(result, "retry completed")
        self.assertEqual(calls, 2)


if __name__ == "__main__":
    unittest.main()
