import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import types
import unittest
from unittest import mock
import uuid


TELEGRAM_PATH = Path(__file__).resolve().parents[1] / "relay" / "herdr_telegram.py"


def _telegram_stubs():
    telegram = types.ModuleType("telegram")

    class InlineKeyboardButton:
        def __init__(self, text, callback_data=None):
            self.text = text
            self.callback_data = callback_data

    class InlineKeyboardMarkup:
        def __init__(self, inline_keyboard):
            self.inline_keyboard = inline_keyboard

    class ForceReply:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    telegram.Update = object
    telegram.ForceReply = ForceReply
    telegram.InlineKeyboardButton = InlineKeyboardButton
    telegram.InlineKeyboardMarkup = InlineKeyboardMarkup

    ext = types.ModuleType("telegram.ext")
    ext.Application = object
    ext.CallbackQueryHandler = object
    ext.CommandHandler = object
    ext.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=object)
    ext.MessageHandler = object
    ext.filters = types.SimpleNamespace()
    return {"telegram": telegram, "telegram.ext": ext}


def loaded_telegram():
    module_name = f"herdr_telegram_test_{uuid.uuid4().hex}"
    with mock.patch.dict(os.environ, {"HERDR_TG_TOKEN": "test"}, clear=False), mock.patch.dict(
        sys.modules, _telegram_stubs(), clear=False
    ), mock.patch.object(sys, "path", [str(TELEGRAM_PATH.parent), *sys.path]):
        spec = importlib.util.spec_from_file_location(module_name, TELEGRAM_PATH)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return types.SimpleNamespace(message_id=42)


class TelegramQuestionTests(unittest.TestCase):
    def test_multi_select_notification_only_offers_safe_output_link(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        app = types.SimpleNamespace(bot=bot)

        asyncio.run(
            telegram.notify_blocked(
                app,
                pane_id="pane-1",
                agent="omp",
                project="demo",
                prompt="Which capabilities?",
                options=["Color", "Font"],
                multi=True,
            )
        )

        markup = bot.sent[0]["reply_markup"]
        self.assertEqual(
            [button.text for row in markup.inline_keyboard for button in row],
            ["≡ Details", "✎ Reply"],
        )
        self.assertIn("web terminal or manual terminal controls", bot.sent[0]["text"])
        self.assertEqual(telegram.pending[(1, 42)], "pane-1")


class TelegramApprovalCardTests(unittest.TestCase):
    def _notify(self, telegram, bot, **overrides):
        kwargs = dict(
            pane_id="pane-1", agent="opencode", project="checkwiseweb",
            prompt="raw terminal tail", options=["Allow once", "Allow always", "Reject", "Dismiss"],
            prompt_id="pid-1", interaction="opencode_permission", host="work-mac",
            cwd="/Users/dmonster/saas/checkwiseweb",
            summary="Bash command npm run build", detail=["$ npm run build"],
        )
        kwargs.update(overrides)
        asyncio.run(telegram.notify_blocked(types.SimpleNamespace(bot=bot), **kwargs))

    def test_permission_notification_is_compact_card(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        self._notify(telegram, bot)

        sent = bot.sent[0]
        self.assertEqual(sent["parse_mode"], "HTML")
        text = sent["text"]
        self.assertIn("◆ <b>Approval</b> · <b>checkwiseweb</b> · work-mac", text)
        self.assertIn("<code>~/saas/checkwiseweb</code>", text)
        self.assertIn("🟢 Bash command", text)
        self.assertIn("Target\n<code>npm run build</code>", text)
        self.assertNotIn("Patterns", text)
        self.assertIn("‖ Waiting", text)
        self.assertNotIn("Use an approval button", text)
        self.assertNotIn("```", text)

        rows = sent["reply_markup"].inline_keyboard
        self.assertEqual([button.text for button in rows[0]], ["✓ Allow once"])
        self.assertEqual([button.text for button in rows[1]], ["∞ Allow always", "⌂ Trust project"])
        self.assertEqual([button.text for button in rows[2]], ["✕ Reject", "≡ Details", "✎ Reply"])
        self.assertEqual(
            telegram.parse_callback_data(rows[0][0].callback_data)["action"], "approve")
        self.assertEqual(telegram.approval_messages["pane-1"]["project"], "checkwiseweb")

    def test_directory_permission_renders_target_and_patterns(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        self._notify(
            telegram, bot,
            summary="Access external directory ~/saas/nodehr",
            detail=["Patterns", "- /Users/dmonster/saas/nodehr/*",
                    "- /var/folders/xx/opencode/nodehr-head/apps/*"],
        )
        text = bot.sent[0]["text"]
        self.assertIn("🟡 External directory access", text)
        self.assertIn("Target\n<code>~/saas/nodehr</code>", text)
        self.assertIn("Patterns", text)
        self.assertIn("• <code>~/saas/nodehr/*</code>", text)
        self.assertIn("• <code>/var/folders/xx/opencode/nodehr-head/apps/*</code>", text)

    def test_dangerous_subject_gets_red_severity(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        self._notify(telegram, bot, summary="Bash command clean build dir",
                     detail=["$ rm -rf build"])
        self.assertIn("🔴 Bash command", bot.sent[0]["text"])

    def test_matched_rule_auto_allows_without_notifying(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        rules = [{"project": "checkwiseweb", "subject": "npm run build"}]
        with mock.patch.object(telegram.approval_rules, "load", return_value=rules), \
                mock.patch.object(telegram, "send_to_relay", mock.AsyncMock()) as respond:
            asyncio.run(telegram.notify_blocked_safely(types.SimpleNamespace(bot=bot), {
                "type": "blocked", "pane_id": "pane-1", "agent": "opencode",
                "project": "checkwiseweb", "prompt": "...",
                "options": ["Allow once"], "prompt_id": "pid-1",
                "interaction": "opencode_permission",
                "summary": "Bash command npm run build", "detail": ["$ npm run build"],
            }))
        respond.assert_awaited_once_with("pane-1", "Allow once", prompt_id="pid-1")
        self.assertEqual(bot.sent, [])

    def test_dangerous_subject_never_auto_approves(self):
        telegram = loaded_telegram()
        telegram.CHAT_ID = "1"
        bot = _Bot()
        rules = [{"project": "checkwiseweb", "subject": "rm"}]
        with mock.patch.object(telegram.approval_rules, "load", return_value=rules):
            self._notify_rule_check(telegram, bot)
        self.assertEqual(len(bot.sent), 1)

    def _notify_rule_check(self, telegram, bot):
        with mock.patch.object(telegram, "send_to_relay", mock.AsyncMock()):
            asyncio.run(telegram.notify_blocked_safely(types.SimpleNamespace(bot=bot), {
                "type": "blocked", "pane_id": "pane-1", "agent": "opencode",
                "project": "checkwiseweb", "prompt": "...",
                "options": ["Allow once"], "prompt_id": "pid-1",
                "interaction": "opencode_permission",
                "summary": "Bash command clean", "detail": ["$ rm -rf build"],
            }))


if __name__ == "__main__":
    unittest.main()
