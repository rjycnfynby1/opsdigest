import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from telethon import types, errors
from opsdigest.messenger.chat_manage import execute


class MembershipTests(unittest.IsolatedAsyncioTestCase):
    def live(self):
        client = AsyncMock()
        client.get_entity.side_effect = lambda x: types.User(id=9) if isinstance(x, str) else types.Chat(id=x, title="Example", photo=types.ChatPhotoEmpty(), participants_count=2, date=None, version=1)
        return Mock(client=client)

    async def test_leave_and_skip_private(self):
        live = self.live()
        live.client.get_entity.side_effect = [types.User(id=2), types.Chat(id=3, title="Example", photo=types.ChatPhotoEmpty(), participants_count=2, date=None, version=1)]
        with patch("opsdigest.messenger.chat_manage.asyncio.sleep", new=AsyncMock()):
            result = await execute(live, [2, 3], "leave", "")
        self.assertEqual([r["status"] for r in result["rows"]], ["skipped", "success"])
        self.assertEqual(live.client.await_count, 1)

    async def test_existing_member_is_not_invited(self):
        live = self.live()
        result = await execute(live, [3], "invite", "example")
        self.assertEqual(result["rows"][0]["status"], "already")
        live.client.assert_not_awaited()

    async def test_flood_marks_remaining_without_requests(self):
        live = self.live()
        live.client.side_effect = errors.FloodWaitError(request=None, capture=60)
        with patch("opsdigest.messenger.chat_manage.asyncio.sleep", new=AsyncMock()):
            result = await execute(live, [3, 4], "leave", "")
        self.assertEqual([r["status"] for r in result["rows"]], ["failed", "skipped"])
        self.assertEqual(live.client.get_entity.await_count, 1)

    async def test_privacy_failure_has_manual_next_step(self):
        live = self.live()
        live.client.get_permissions.side_effect = errors.UserNotParticipantError(request=None)
        live.client.side_effect = errors.UserPrivacyRestrictedError(request=None)
        with patch("opsdigest.messenger.chat_manage.asyncio.sleep", new=AsyncMock()):
            result = await execute(live, [3], "invite", "example")
        row = result["rows"][0]
        self.assertEqual(row["status"], "failed")
        self.assertIn("администратора", row["detail"])
