#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-telegram-bot>=21.0", "websockets>=14.0"]
# ///
import asyncio
import ast
import importlib
import json
import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "relay"))
os.environ.setdefault("HERDR_TG_TOKEN", "test-token")

tg = importlib.import_module("herdr_telegram")


def make_agents(count, *, status="idle", project="project"):
    return [
        {
            "pane_id": f"w{i}:p1",
            "agent": "opencode",
            "status": status,
            "project": project,
            "cwd": f"/work/{project}/{i}",
            "host": "local",
        }
        for i in range(count)
    ]


class FakeMessage:
    def __init__(self, chat_id=42, chat_type="private", message_id=10):
        self.replies = []
        self.message_id = message_id
        self.chat_id = chat_id
        self.chat = SimpleNamespace(id=chat_id, type=chat_type)
        self.reply_markup = None
        self.reply_to_message = None
        self.text = ""

    async def reply_text(self, text, **kwargs):
        sent = SimpleNamespace(message_id=self.message_id * 100 + len(self.replies), chat_id=self.chat_id)
        self.replies.append((text, kwargs, sent))
        return sent


class FakeCallback:
    def __init__(self, data, chat_id=42, chat_type="private", message_id=10):
        self.data = data if isinstance(data, str) else json.dumps(data, separators=(",", ":"))
        self.message = FakeMessage(chat_id, chat_type, message_id)
        self.answers = []
        self.edited_markup = None
        self.edit_calls = 0

    async def answer(self, text=None):
        self.answers.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.edit_calls += 1
        self.edited_markup = reply_markup

    async def edit_message_text(self, text=None, **kwargs):
        self.edited_text = text
        self.edited_text_kwargs = kwargs


class FakeBot:
    def __init__(self):
        self.sent = []
        self.edits = []

    async def send_message(self, chat_id, text, **kwargs):
        message = SimpleNamespace(message_id=500 + len(self.sent), chat_id=chat_id)
        self.sent.append((chat_id, text, kwargs, message))
        return message

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id, text, kwargs))
        return True


class FakeRelayConnection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def send(self, message):
        self.sent.append(json.loads(message))

    async def recv(self):
        return json.dumps(next(self.responses))

    def __aiter__(self):
        async def empty_messages():
            if False:
                yield None
        return empty_messages()


def make_update(chat_id=42, chat_type="private", callback=None, message=None):
    if callback is not None:
        callback.message.chat_id = chat_id
        callback.message.chat = SimpleNamespace(id=chat_id, type=chat_type)
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=chat_id, type=chat_type),
        message=message or FakeMessage(chat_id, chat_type),
        callback_query=callback,
    )


def make_active_approval_keyboard(pane_id, options):
    markup = tg.make_keyboard(pane_id, options)
    generation = json.loads(markup.inline_keyboard[0][0].callback_data)["g"]
    tg.approval_tokens[pane_id] = generation
    tg.blocked_prompt_ids[pane_id] = "prompt-123"
    return markup


class TelegramDashboardTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.old_chat_id = tg.CHAT_ID
        tg.CHAT_ID = "42"
        tg.agents = []
        tg.relay_connected = False
        tg.pending.clear()
        tg.approval_tokens.clear()
        tg.blocked_prompt_ids.clear()
        tg.prev_statuses.clear()
        tg.approval_messages.clear()
        tg.working_cards.clear()
        tg.daily_stats.clear()
        fetch_patch = patch.object(tg, "fetch_session_info", AsyncMock(return_value=None))
        fetch_patch.start()
        self.addCleanup(fetch_patch.stop)
        web_patch = patch.object(tg, "_web_base", return_value="")
        web_patch.start()
        self.addCleanup(web_patch.stop)
        tg.daily_stats.clear()

    def tearDown(self):
        tg.CHAT_ID = self.old_chat_id
        tg.agents = []
        tg.relay_connected = False
        tg.approval_tokens.clear()
        tg.prev_statuses.clear()
        tg.daily_stats.clear()

    async def test_start_rejects_unauthorized_chat(self):
        update = make_update(chat_id=7)

        await tg.cmd_start(update, SimpleNamespace(args=[]))

        self.assertEqual(update.message.replies, [])

    async def test_start_preserves_chat_discovery_mode(self):
        tg.CHAT_ID = ""
        update = make_update(chat_id=-123)

        await tg.cmd_start(update, SimpleNamespace(args=[]))

        self.assertIn("Chat ID: -123", update.message.replies[0][0])

    async def test_start_reports_disconnected_and_empty_states(self):
        disconnected = make_update()
        await tg.cmd_start(disconnected, SimpleNamespace(args=[]))
        self.assertIn("disconnected", disconnected.message.replies[0][0].lower())
        self.assertNotIn("reply_markup", disconnected.message.replies[0][1])

        tg.relay_connected = True
        empty = make_update()
        await tg.cmd_start(empty, SimpleNamespace(args=[]))
        self.assertIn("no agents", empty.message.replies[0][0].lower())

    async def test_start_lists_current_sixteen_agent_herd(self):
        tg.relay_connected = True
        tg.agents = make_agents(16)
        update = make_update()

        await tg.cmd_start(update, SimpleNamespace(args=[]))

        markup = update.message.replies[0][1]["reply_markup"]
        agent_buttons = [row[0] for row in markup.inline_keyboard if row[0].text not in ("Previous", "Next")]
        self.assertEqual(len(agent_buttons), 16)
        self.assertTrue(all(tg.parse_callback_data(button.callback_data)["action"] == "select_reply" for button in agent_buttons))

    def test_labels_sort_status_and_disambiguate_duplicate_agents(self):
        agent_list = [
            *make_agents(2, status="idle", project="same"),
            *make_agents(1, status="working", project="work"),
            *make_agents(1, status="blocked", project="blocked"),
        ]
        agent_list[-1]["host"] = "remote.example"

        markup = tg.build_agent_keyboard("read", agent_list=agent_list)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertTrue(labels[0].startswith("[BLOCKED]"))
        self.assertTrue(labels[1].startswith("[WORKING]"))
        self.assertIn("remote.example", labels[0])
        duplicate_labels = [label for label in labels if "same" in label]
        self.assertEqual(len(set(duplicate_labels)), 2)
        self.assertTrue(all("w" in label and ":p1" in label for label in duplicate_labels))

    def test_large_keyboard_paginates_without_omitting_agents(self):
        agent_list = make_agents(tg.AGENT_PAGE_SIZE + 5)
        tg.agents = agent_list

        first = tg.build_agent_keyboard("read", page=0, agent_list=agent_list)
        second = tg.build_agent_keyboard("read", page=1, agent_list=agent_list)
        first_ids = [tg.parse_callback_data(row[0].callback_data)["pane_id"] for row in first.inline_keyboard[:-1]]
        second_ids = [tg.parse_callback_data(row[0].callback_data)["pane_id"] for row in second.inline_keyboard[:-1]]

        self.assertEqual(len(first_ids), tg.AGENT_PAGE_SIZE)
        self.assertEqual(len(second_ids), 5)
        self.assertEqual(len(set(first_ids + second_ids)), tg.AGENT_PAGE_SIZE + 5)

    def test_long_labels_remain_unique_and_preserve_remote_host(self):
        long_prefix = "project-" + "x" * 70
        agent_list = make_agents(3)
        agent_list[0]["project"] = long_prefix + "-one"
        agent_list[1]["project"] = long_prefix + "-two"
        agent_list[2]["project"] = long_prefix + "-three"
        agent_list[2]["host"] = "remote-" + "host" * 30 + ".example"

        markup = tg.build_agent_keyboard("read", agent_list=agent_list)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertEqual(len(set(labels)), 3)
        self.assertTrue(any("@remote-" in label for label in labels))
        self.assertTrue(all(len(label) <= 64 for label in labels))

    def test_compacted_pane_hash_collision_still_has_unique_labels(self):
        agent_list = make_agents(2, project="same")
        agent_list[0]["pane_id"] = "sameprefx-very-long-pane-id-2606"
        agent_list[1]["pane_id"] = "sameprefx-very-long-pane-id-3604"

        markup = tg.build_agent_keyboard("read", agent_list=agent_list)
        labels = [row[0].text for row in markup.inline_keyboard]

        self.assertEqual(len(set(labels)), 2)
        self.assertTrue(all(len(label) <= 64 for label in labels))

    def test_all_pane_callbacks_fit_telegram_byte_limit(self):
        pane_id = "pane-" + "кирилица" * 100
        tg.agents = make_agents(1)
        tg.agents[0]["pane_id"] = pane_id
        markups = [
            tg.build_agent_keyboard(action)
            for action in ("read", "interrupt", "select_send", "select_reply", "trust")
        ]
        markups.extend([tg.make_keyboard(pane_id, None), tg.interaction_keyboard(pane_id)])

        callbacks = [
            button.callback_data
            for markup in markups
            for row in markup.inline_keyboard
            for button in row
        ]
        self.assertTrue(all(len(callback.encode()) <= 64 for callback in callbacks))

    async def test_read_and_filtered_trust_pickers_do_not_truncate(self):
        tg.agents = make_agents(16)
        read_update = make_update()
        await tg.cmd_read(read_update, SimpleNamespace(args=[]))
        self.assertEqual(len(read_update.message.replies[0][1]["reply_markup"].inline_keyboard), 16)

        tg.agents = make_agents(12, status="blocked") + make_agents(3, status="idle")
        trust_update = make_update()
        await tg.cmd_trust(trust_update, SimpleNamespace(args=[]))
        self.assertEqual(len(trust_update.message.replies[0][1]["reply_markup"].inline_keyboard), 12)

    async def test_direct_read_send_and_reply_forms_preserve_behavior(self):
        tg.agents = make_agents(1)
        read_update = make_update()
        send_update = make_update()
        reply_update = make_update()

        with (
            patch.object(tg, "read_pane", AsyncMock(side_effect=["read output", "reply output"])),
            patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text,
        ):
            await tg.cmd_read(read_update, SimpleNamespace(args=["project"]))
            await tg.cmd_send(send_update, SimpleNamespace(args=["project", "hello"]))
            await tg.cmd_reply(reply_update, SimpleNamespace(args=["project"]))

        self.assertIn("read output", read_update.message.replies[0][0])
        send_text.assert_awaited_once_with("w0:p1", "hello")
        self.assertIn("Sent to project", send_update.message.replies[0][0])
        reply_text, reply_kwargs, reply_message = reply_update.message.replies[0]
        self.assertIn("reply output", reply_text)
        self.assertIsInstance(reply_kwargs["reply_markup"], tg.ForceReply)
        self.assertEqual(tg.pending_pane(42, reply_message.message_id), "w0:p1")

    async def test_read_output_is_styled_and_escaped_html(self):
        # Arbitrary agent text must arrive escaped, with a glanceable header.
        tg.agents = make_agents(1, status="working")
        read_update = make_update()

        with patch.object(tg, "read_pane", AsyncMock(return_value="fixed <bug> & shipped")):
            await tg.cmd_read(read_update, SimpleNamespace(args=["project"]))

        text, kwargs = read_update.message.replies[0][0], read_update.message.replies[0][1]
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertIn("<b>project</b>", text)
        self.assertIn("<i>WORKING</i>", text)
        self.assertIn("fixed &lt;bug&gt; &amp; shipped", text)

    async def test_send_and_reply_pickers_target_the_expected_actions(self):
        tg.agents = make_agents(1)
        send_update = make_update()
        reply_update = make_update()

        await tg.cmd_send(send_update, SimpleNamespace(args=[]))
        await tg.cmd_reply(reply_update, SimpleNamespace(args=[]))

        send_button = send_update.message.replies[0][1]["reply_markup"].inline_keyboard[0][0]
        reply_button = reply_update.message.replies[0][1]["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(tg.parse_callback_data(send_button.callback_data)["action"], "select_send")
        self.assertEqual(tg.parse_callback_data(reply_button.callback_data)["action"], "select_reply")

    async def test_done_agent_is_listed_and_sends_finished_notification(self):
        tg.agents = make_agents(1, status="done", project="completed")
        update = make_update()

        await tg.cmd_agents(update, SimpleNamespace(args=[]))

        self.assertIn("DONE:", update.message.replies[0][0])
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "working"
        with patch.object(
            tg, "read_pane",
            AsyncMock(return_value="Build completed successfully.\n3 files changed, tests passed."),
        ):
            await tg.track_agent_updates(app, tg.agents)
        chat_id, text, kwargs, sent = bot.sent[0]
        self.assertEqual(chat_id, 42)
        self.assertIn("✅ <b>completed</b> finished", text)
        self.assertIn("3 files changed, tests passed.", text)
        self.assertIn("<code>/work/completed/0</code>", text)
        button = kwargs["reply_markup"].inline_keyboard[0][0]
        self.assertEqual(button.text, "👁 Details")
        self.assertEqual(tg.parse_callback_data(button.callback_data)["pane_id"], "w0:p1")
        self.assertEqual(tg.pending_pane(42, sent.message_id), "w0:p1")

    async def test_failed_completion_shows_error_tail(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "working"
        agents = make_agents(1, status="idle", project="broken")
        with patch.object(
            tg, "read_pane",
            AsyncMock(return_value="running tests...\nnpm test failed\n2 tests failed in auth.spec.ts"),
        ):
            await tg.track_agent_updates(app, agents)
        text = bot.sent[0][1]
        self.assertIn("❌ <b>broken</b> failed", text)
        self.assertIn("2 tests failed in auth.spec.ts", text)

    async def test_zero_failures_still_counts_as_success(self):
        failed, _lines, _hidden = tg.completion_summary("✓ 42 passed, 0 failed")
        self.assertFalse(failed)

    async def test_long_output_is_truncated_with_count(self):
        content = "\n".join(f"line {i}" for i in range(30))
        failed, lines, hidden = tg.completion_summary(content)
        self.assertFalse(failed)
        self.assertEqual(len(lines), 4)
        self.assertEqual(hidden, 26)
        text = tg.finished_text({"project": "p", "host": "local", "cwd": "/w/p"},
                                14, lines, hidden, failed)
        self.assertIn("… 26 more lines", text)
        self.assertIn("finished · 14s", text)

    async def test_empty_output_says_so(self):
        text = tg.finished_text({"project": "quiet", "host": "local", "cwd": ""},
                                3, [], 0, False)
        self.assertIn("Completed with no output.", text)

    async def test_working_session_gets_live_card(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        chat_id, text, kwargs, sent = bot.sent[0]
        self.assertIn("⚙️ <b>live</b> · working", text)
        self.assertIn("<code>/work/live/0</code>", text)
        buttons = [b.text for b in kwargs["reply_markup"].inline_keyboard[0]]
        self.assertEqual(buttons, ["⛔ Interrupt", "👁 Details"])
        self.assertEqual(tg.pending_pane(chat_id, sent.message_id), "w0:p1")
        self.assertEqual(len(bot.edits), 0)

    async def test_working_card_edit_fires_on_title_change_only(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        agents = make_agents(1, status="working", project="live")
        await tg.track_agent_updates(app, agents)
        # same title, within the minute: silent
        await tg.track_agent_updates(app, agents)
        self.assertEqual(len(bot.edits), 0)
        # new activity title: edit in place
        agents[0]["title"] = "Fixing auth token refresh"
        await tg.track_agent_updates(app, agents)
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("Fixing auth token refresh", bot.edits[0][2])

    async def test_working_card_pauses_while_blocked(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        tg.prev_statuses["w0:p1"] = "working"
        await tg.track_agent_updates(app, make_agents(1, status="blocked", project="live"))
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("⏸ <b>live</b> · waiting for approval", bot.edits[0][2])

    async def test_finish_edits_the_working_card_not_a_new_message(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        self.assertEqual(len(bot.sent), 1)
        tg.prev_statuses["w0:p1"] = "working"
        with patch.object(
            tg, "read_pane",
            AsyncMock(return_value="Build completed successfully.\n3 files changed."),
        ):
            await tg.track_agent_updates(app, make_agents(1, status="done", project="live"))
        self.assertEqual(len(bot.sent), 1)          # no new message
        self.assertEqual(len(bot.edits), 1)         # the card became the result
        self.assertIn("✅ <b>live</b> finished", bot.edits[0][2])
        # The rolling card stays as this pane's slot, now in finished state.
        self.assertEqual(tg.working_cards["w0:p1"]["state"], "finished")

    async def test_flap_reuses_card_within_grace(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        self.assertEqual(len(bot.sent), 1)
        tg.prev_statuses["w0:p1"] = "working"
        with patch.object(tg, "read_pane", AsyncMock(return_value="ok")):
            await tg.track_agent_updates(app, make_agents(1, status="done", project="live"))
        self.assertEqual(len(bot.sent), 1)
        # The agent restarts within the grace window: same card, flipped back.
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        self.assertEqual(len(bot.sent), 1)          # still one message
        self.assertEqual(len(bot.edits), 2)         # finish + resume
        self.assertIn("⚙️ <b>live</b> · working", bot.edits[1][2])

    async def test_restart_beyond_grace_gets_a_new_card(self):
        import time
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        with patch.object(tg, "read_pane", AsyncMock(return_value="ok")):
            tg.prev_statuses["w0:p1"] = "working"
            await tg.track_agent_updates(app, make_agents(1, status="done", project="live"))
        tg.working_cards["w0:p1"]["finished_at"] = time.time() - 999
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, make_agents(1, status="working", project="live"))
        self.assertEqual(len(bot.sent), 2)          # genuinely new session: new card
        self.assertEqual(tg.working_cards["w0:p1"]["state"], "working")

    async def test_stale_approval_gets_one_nudge(self):
        import time
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.approval_messages["w0:p1"] = {
            "chat_id": 42, "message_id": 9, "project": "slow", "host": "local",
            "cwd": "", "branch": "", "summary": "s", "detail": [],
            "created": time.time() - 400,
        }
        tg.prev_statuses["w0:p1"] = "working"
        await tg.track_agent_updates(app, make_agents(1, status="blocked", project="slow"))
        self.assertEqual(len(bot.sent), 1)
        self.assertIn("waiting for approval", bot.sent[0][1])
        # Later snapshots never repeat the nudge.
        tg.prev_statuses["w0:p1"] = "blocked"
        await tg.track_agent_updates(app, make_agents(1, status="blocked", project="slow"))
        self.assertEqual(len(bot.sent), 1)

    async def test_stuck_session_flips_card_and_pings_once(self):
        import time
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        agents = make_agents(1, status="working", project="stuck-proj")
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(app, agents)
        card = tg.working_cards["w0:p1"]
        stale = {"updated_ms": (time.time() - 700) * 1000, "activity": "Bash: x",
                 "todos": [], "context_tokens": 0, "cost": 0, "model_id": "", "branch": ""}
        card["last_edit"] = time.time() - 61          # force the minute tick
        with patch.object(tg, "fetch_session_info", AsyncMock(return_value=stale)):
            await tg.track_agent_updates(app, agents)
        self.assertEqual(len(bot.edits), 1)
        self.assertIn("possibly stuck", bot.edits[0][2])
        self.assertEqual(len(bot.sent), 2)            # working card + the nudge
        self.assertIn("possibly stuck", bot.sent[1][1])
        card["last_edit"] = time.time() - 61
        with patch.object(tg, "fetch_session_info", AsyncMock(return_value=stale)):
            await tg.track_agent_updates(app, agents)
        self.assertEqual(len(bot.sent), 2)            # same staleness: no repeat

    def test_seconds_until_rolls_to_tomorrow(self):
        import time
        now = time.mktime(time.strptime("2026-09-13 10:00", "%Y-%m-%d %H:%M"))
        self.assertAlmostEqual(tg.seconds_until("11:00", now), 3600, delta=5)
        self.assertAlmostEqual(tg.seconds_until("09:00", now), 23 * 3600, delta=5)

    def test_digest_text_includes_auto_approvals(self):
        tg.daily_stats["p1"] = {"project": "nodehr", "agent": "opencode",
                                "working_mins": 12, "blocked_count": 2, "auto_approved": 4}
        text = tg.digest_text()
        self.assertIn("nodehr (opencode): 12m working, blocked 2x, auto-approved 4x", text)

    async def test_dangerous_approval_requires_second_press(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked", project="danger")
        gen = "abcd1234"
        tg.approval_tokens["w0:p1"] = gen
        tg.blocked_prompt_ids["w0:p1"] = "pid1"
        tg.approval_messages["w0:p1"] = {
            "chat_id": 42, "message_id": 10, "project": "danger", "host": "local",
            "cwd": "/w/danger", "branch": "main", "summary": "Bash command clean",
            "detail": ["$ rm -rf build"], "created": 1.0,
        }
        ctx = SimpleNamespace(bot=FakeBot())
        with patch.object(tg, "send_to_relay", AsyncMock()) as respond:
            first = FakeCallback(tg.pane_callback_data("approve", "w0:p1", g=gen))
            await tg.handle_callback(make_update(callback=first), ctx)
            respond.assert_not_awaited()
            self.assertIn("press again to confirm", ctx.bot.edits[0][2])

            second = FakeCallback(tg.pane_callback_data("approve", "w0:p1", g=gen))
            await tg.handle_callback(make_update(callback=second), ctx)
            respond.assert_awaited_once_with("w0:p1", "Allow once", prompt_id="pid1")

    async def test_quiet_project_cards_arrive_silently(self):
        with patch.object(tg, "load_quiet", return_value=["watchtube"]):
            bot = FakeBot()
            app = SimpleNamespace(bot=bot)
            tg.prev_statuses["w0:p1"] = "idle"
            await tg.track_agent_updates(
                app, make_agents(1, status="working", project="watchtube"))
            self.assertTrue(bot.sent[0][2].get("disable_notification"))
        tg.working_cards.clear()
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "idle"
        await tg.track_agent_updates(
            app, make_agents(1, status="working", project="loud"))
        self.assertFalse(bot.sent[0][2].get("disable_notification"))

    async def test_quiet_command_toggles_membership(self):
        with patch.object(tg, "load_quiet", return_value=["watchtube"]), \
                patch.object(tg, "save_quiet") as save:
            update = make_update()
            await tg.cmd_quiet(update, SimpleNamespace(args=["nodehr"]))
            save.assert_called_once_with(["watchtube", "nodehr"])
            self.assertIn("added", update.message.replies[0][0])

            update = make_update()
            await tg.cmd_quiet(update, SimpleNamespace(args=["watchtube"]))
            save.assert_called_with(["nodehr"])
            self.assertIn("removed", update.message.replies[0][0])

    def test_web_button_appears_only_with_hostname(self):
        with patch.object(tg, "_web_base", return_value="https://relay.example.com"):
            keyboard = tg.working_keyboard("w0:p1")
            web = [b for row in keyboard.inline_keyboard for b in row if b.text == "🌐 Web"]
            self.assertEqual(len(web), 1)
            self.assertEqual(web[0].url, "https://relay.example.com/?pane=w0:p1")
        with patch.object(tg, "_web_base", return_value=""):
            keyboard = tg.finished_keyboard("w0:p1", failed=True)
            texts = [b.text for row in keyboard.inline_keyboard for b in row]
            self.assertEqual(texts, ["🔁 Retry", "👁 Details", "💬 Reply"])

    async def test_now_lists_one_card_per_active_session(self):
        tg.relay_connected = True
        tg.agents = make_agents(3, status="idle")
        tg.agents[0].update(status="working", project="alpha", pane_id="w0:p1")
        tg.agents[2].update(status="blocked", project="beta", pane_id="w2:p1")
        info = {
            "context_tokens": 217447, "created_ms": 1789300000000, "cost": 0.0,
            "model_id": "glm-5.3",
            "todos": [
                {"status": "in_progress", "content": "Backend: portal login"},
                {"status": "pending", "content": "Frontend: feature module"},
                {"status": "completed", "content": "Setup"},
            ],
        }
        with patch.object(tg, "fetch_session_info", AsyncMock(return_value=info)):
            update = make_update()
            await tg.cmd_now(update, SimpleNamespace())

        replies = update.message.replies
        self.assertEqual(len(replies), 2)  # idle agent is not listed
        first_text, first_kwargs, _ = replies[0]
        second_text, second_kwargs, _ = replies[1]
        self.assertIn("⏸ <b>beta</b> · waiting for approval", first_text)   # blocked sorts first
        self.assertIn("⚙️ <b>alpha</b> · working", second_text)
        self.assertIn("Context: 217,447 tokens · 22%", second_text)
        self.assertIn("[•] Backend: portal login", second_text)
        self.assertIn("[ ] Frontend: feature module", second_text)
        self.assertIn("Todo 1/3", second_text)
        for kwargs in (first_kwargs, second_kwargs):
            buttons = [b.text for b in kwargs["reply_markup"].inline_keyboard[0]]
            self.assertEqual(buttons, ["⛔ Interrupt", "👁 Details"])

    def test_session_details_truncate_todos_and_hide_unknown_model_pct(self):
        info = {
            "context_tokens": 5000, "created_ms": 0, "cost": 1.5,
            "model_id": "mystery-model",
            "todos": [{"status": "pending", "content": f"task {i}"} for i in range(9)],
        }
        lines = tg.session_details_lines(info)
        self.assertIn("Context: 5,000 tokens", lines[0])
        self.assertNotIn("%", lines[0])
        self.assertIn("$1.50", lines[1])
        self.assertIn("Todo 0/9", lines)
        self.assertIn("+3 more", lines)
        self.assertEqual(tg.session_details_lines(None), [])

    async def test_now_with_nothing_active_says_so(self):
        tg.relay_connected = True
        tg.agents = make_agents(2, status="idle")
        update = make_update()

        await tg.cmd_now(update, SimpleNamespace())

        self.assertEqual(update.message.replies[0][0], "No active sessions.")

    async def test_now_shows_activity_and_branch_from_session_info(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="working", project="alpha")
        info = {"activity": "Bash: npm test", "branch": "main", "context_tokens": 0,
                "todos": [], "cost": 0, "model_id": "", "created_ms": 0}
        with patch.object(tg, "fetch_session_info", AsyncMock(return_value=info)):
            update = make_update()
            await tg.cmd_now(update, SimpleNamespace())
        text = update.message.replies[0][0]
        self.assertIn("<code>/work/alpha/0</code> · <i>main</i>", text)
        self.assertIn("Bash: npm test", text)

    def test_context_line_joins_cwd_and_branch(self):
        line = tg.context_line("/Users/dmonster/saas/nodehr", "feature/payroll")
        self.assertEqual(line, "<code>~/saas/nodehr</code> · <i>feature/payroll</i>")
        self.assertEqual(tg.context_line("", "main"), "<i>main</i>")
        self.assertEqual(tg.context_line("/w/p", ""), "<code>/w/p</code>")

    async def test_rules_lists_and_deletes(self):
        tg.relay_connected = True
        rules = [
            {"project": "nodehr", "subject": "npm run build"},
            {"project": "checkwiseweb"},
        ]
        with patch.object(tg.approval_rules, "load", return_value=rules), \
                patch.object(tg.approval_rules, "save") as save:
            update = make_update()
            await tg.cmd_rules(update, SimpleNamespace())
            text, kwargs, _ = update.message.replies[0]
            self.assertIn("Approval rules (2)", text)
            self.assertIn("1. nodehr · always · npm run build", text)
            self.assertIn("2. checkwiseweb · project · all safe actions", text)
            buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
            self.assertEqual([b.text for b in buttons], ["🗑 1", "🗑 2"])

            callback = FakeCallback({"action": "rule_del", "i": 0})
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())
            save.assert_called_once_with([{"project": "checkwiseweb"}])
            self.assertIn("Approval rules (1)", callback.edited_text)

    async def test_finished_card_notes_auto_approvals(self):
        bot = FakeBot()
        app = SimpleNamespace(bot=bot)
        tg.prev_statuses["w0:p1"] = "working"
        tg.daily_stats["w0:p1"] = {"agent": "opencode", "project": "auto",
                                   "blocked_count": 0, "working_mins": 0,
                                   "last_change": 0, "auto_approved": 2}
        tg.working_cards.pop("w0:p1", None)
        with patch.object(tg, "read_pane", AsyncMock(return_value="ok")):
            await tg.track_agent_updates(app, make_agents(1, status="done", project="auto"))
        self.assertIn("🔇 2 auto-approved by rules", bot.sent[0][1])

    async def test_page_callback_rebuilds_from_latest_cache(self):
        tg.relay_connected = True
        tg.agents = make_agents(tg.AGENT_PAGE_SIZE + 5)
        callback = FakeCallback({"action": "page", "menu": "read", "page": 1})

        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        self.assertEqual(len(callback.edited_markup.inline_keyboard), 6)

    async def test_dashboard_selection_creates_private_reply_prompt(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        button = tg.build_agent_keyboard("select_reply").inline_keyboard[0][0]
        callback = FakeCallback(button.callback_data)

        with patch.object(tg, "read_pane", AsyncMock(return_value="recent output")):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        text, kwargs, sent = callback.message.replies[0]
        self.assertIn("recent output", text)
        self.assertIsInstance(kwargs["reply_markup"], tg.ForceReply)
        self.assertEqual(kwargs["reply_markup"].input_field_placeholder, "Reply to project")
        self.assertEqual(tg.pending_pane(42, sent.message_id), "w0:p1")

    async def test_stale_selection_offers_refresh(self):
        tg.relay_connected = True
        callback = FakeCallback({"action": "select_reply", "pane_id": "gone:pane"})

        await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        text, kwargs, _ = callback.message.replies[0]
        self.assertIn("no longer available", text.lower())
        self.assertIn("reply_markup", kwargs)

    async def test_disconnected_callback_cannot_use_stale_agent_cache(self):
        tg.agents = make_agents(1)
        callback = FakeCallback({"action": "select_reply", "pane_id": "w0:p1"})

        with patch.object(tg, "read_pane", AsyncMock()) as read_pane:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        read_pane.assert_not_awaited()
        self.assertIn("disconnected", callback.message.replies[0][0].lower())

    async def test_callback_rechecks_command_eligibility(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="idle")
        callback = FakeCallback({"action": "trust", "pane_id": "w0:p1"})

        with patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_not_awaited()
        self.assertIn("no longer available", callback.message.replies[0][0].lower())

    async def test_stale_approval_cannot_type_into_unblocked_agent(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup
        tg.agents[0]["status"] = "working"

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_not_awaited()
        self.assertIn("no longer available", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)

    async def test_approval_transport_failure_preserves_controls(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock(side_effect=OSError("offline"))):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        self.assertIn("failed", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)
        self.assertIn("w0:p1", tg.approval_tokens)

    async def test_approval_sends_key_and_removes_controls_on_success(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        markup = make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_awaited_once_with("w0:p1", ["1"], prompt_id="prompt-123")
        self.assertEqual(callback.edit_calls, 1)
        self.assertIn("Sent: yes", callback.message.replies[0][0])
        self.assertNotIn("w0:p1", tg.approval_tokens)

    async def test_approval_from_previous_blocked_prompt_is_rejected(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        old_markup = make_active_approval_keyboard("w0:p1", ["old yes", "old no"])
        make_active_approval_keyboard("w0:p1", ["new yes", "new no"])
        callback = FakeCallback(old_markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = old_markup

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_not_awaited()
        self.assertIn("older prompt", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)

    async def test_legacy_approval_is_rejected_without_discarding_controls(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        make_active_approval_keyboard("w0:p1", ["yes", "no"])
        callback = FakeCallback({"pane_id": "w0:p1", "k": "1"})

        with patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_keys.assert_not_awaited()
        self.assertIn("older prompt", callback.message.replies[0][0].lower())
        self.assertEqual(callback.edit_calls, 0)

    async def test_send_keys_requires_positive_relay_acknowledgement(self):
        accepted = FakeRelayConnection([
            {"type": "agents", "agents": []},
            {"type": "command_result", "command": "send_keys", "ok": True, "request_id": "request-123"},
        ])
        with patch("websockets.connect", return_value=accepted), patch.object(
            tg.secrets, "token_hex", return_value="request-123"
        ):
            await tg.send_keys_to_relay("w0:p1", ["1"])
        self.assertEqual(accepted.sent, [{
            "type": "send_keys",
            "pane_id": "w0:p1",
            "keys": ["1"],
            "request_id": "request-123",
        }])

        rejected = FakeRelayConnection([{"type": "error", "message": "keys contain disallowed values"}])
        with patch("websockets.connect", return_value=rejected):
            with self.assertRaisesRegex(RuntimeError, "disallowed"):
                await tg.send_keys_to_relay("w0:p1", ["1"])

    async def test_read_pane_skips_arbitrarily_many_unrelated_messages(self):
        # The connect preamble (sessions, agents, one `blocked` per blocked
        # agent) can put any number of messages ahead of pane_content. A
        # fixed-count skip budget breaks once there are enough blocked
        # agents; the skip must be bounded by time, not by a message count.
        preamble = [{"type": "sessions", "sources": []}, {"type": "agents", "agents": []}]
        preamble += [{"type": "blocked", "pane_id": f"w{i}:p1"} for i in range(8)]
        preamble.append({"type": "pane_content", "content": "hello from pane"})
        connection = FakeRelayConnection(preamble)

        with patch("websockets.connect", return_value=connection):
            content = await tg.read_pane("w0:p1")

        self.assertEqual(content, "hello from pane")

    async def test_read_pane_strips_tui_chrome_lines(self):
        # pane_content is a rendered TUI frame: box borders, opencode's `┃` rail
        # (user input + tool blocks), status/thought/footer markers must not
        # reach the chat -- only the agent's plain reply text.
        frame = "\n".join([
            "┃ ╹▀▀▀▀▀▀▀▀▀▀…",
            "┃────────────┃",
            "  ┃  $ tail -4 ~/Library/Logs/herdr-remote/relay.log",
            "  ┃  2026-09-02 20:52:08 [INFO] mDNS registering at 127.0.0.1",
            "  ┃  look at this new response",
            "  + Thought: 2.0s",
            "  ▣  Build · GLM-5.3 · 48.5s",
            "  ⚙️ codedb_codedb_read [path=relay/herdr_relay.py]",
            "  /relay  59.3K (6%)  ctrl+p commands",
            "",
            "  Yes — now it works. Both services restarted with the fix.",
        ])
        connection = FakeRelayConnection([{"type": "pane_content", "content": frame}])

        with patch("websockets.connect", return_value=connection):
            content = await tg.read_pane("w0:p1")

        self.assertEqual(content, "Yes — now it works. Both services restarted with the fix.")
        # The viewport is read, not scrollback: old tool output and the
        # multi-second `recent` harvest stay out of the phone view.
        self.assertEqual(connection.sent[0]["source"], "visible")

    async def test_session_lists_marks_active_and_switches(self):
        list_update = make_update()
        preamble = [{
            "type": "sessions",
            "sources": [{
                "host": "local", "active": "main",
                "sessions": [{"name": "main", "running": True},
                             {"name": "scratch", "running": False}],
            }],
        }]
        with patch("websockets.connect", return_value=FakeRelayConnection(preamble)):
            await tg.cmd_session(list_update, SimpleNamespace(args=[]))
        text, kwargs, _ = list_update.message.replies[0]
        self.assertEqual(kwargs.get("parse_mode"), "HTML")
        self.assertIn("<b>local</b>", text)
        self.assertIn("▶ main", text)
        self.assertIn("scratch (stopped)", text)

        # One argument switches the local source (no host key); two name a host.
        ok = [{"type": "command_result", "command": "session_switch", "ok": True,
               "request_id": "request-123"}]
        local_switch, remote_switch = make_update(), make_update()
        with patch.object(tg.secrets, "token_hex", return_value="request-123"):
            with patch("websockets.connect", return_value=FakeRelayConnection(list(ok))) as local_ws:
                await tg.cmd_session(local_switch, SimpleNamespace(args=["scratch"]))
            self.assertEqual(local_ws.return_value.sent[0]["type"], "session_switch")
            self.assertEqual(local_ws.return_value.sent[0]["session"], "scratch")
            self.assertNotIn("host", local_ws.return_value.sent[0])
            self.assertIn("Switched local to scratch", local_switch.message.replies[0][0])

            with patch("websockets.connect", return_value=FakeRelayConnection(list(ok))) as remote_ws:
                await tg.cmd_session(remote_switch, SimpleNamespace(args=["pi@desk", "main"]))
            self.assertEqual(remote_ws.return_value.sent[0]["host"], "pi@desk")
            self.assertEqual(remote_ws.return_value.sent[0]["session"], "main")

        # A relay rejection surfaces as a switch failure, not a crash.
        rejected = [{"type": "error", "message": "unknown session: nope"}]
        failed_update = make_update()
        with patch("websockets.connect", return_value=FakeRelayConnection(rejected)):
            await tg.cmd_session(failed_update, SimpleNamespace(args=["nope"]))
        self.assertIn("unknown session: nope", failed_update.message.replies[0][0])

    def test_relay_allows_numeric_approval_keys_and_acknowledges_them(self):
        relay_path = ROOT / "relay" / "herdr_relay.py"
        source = relay_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        safe_keys = next(
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "SAFE_KEYS" for target in node.targets)
        )
        values = eval(compile(ast.Expression(safe_keys), str(relay_path), "eval"), {"range": range})
        self.assertTrue({"1", "2", "3"}.issubset(values))
        self.assertIn('"command": "send_keys"', source)
        self.assertIn('"ok": True', source)
        self.assertIn("if result.returncode != 0:", source)

    async def test_omp_choice_uses_question_response_with_label_and_prompt_id(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.agents[0]["agent"] = "omp"
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        markup = tg.make_keyboard(
            "w0:p1",
            ["Red", "Blue"],
            generation="generation",
            interaction="omp_question",
        )
        tg.approval_tokens["w0:p1"] = "generation"
        callback = FakeCallback(markup.inline_keyboard[1][0].callback_data)
        callback.message.reply_markup = markup

        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay,
            patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys,
        ):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_awaited_once_with("w0:p1", "Blue", prompt_id="prompt-123")
        send_keys.assert_not_awaited()

    async def test_opencode_permission_buttons_send_option_text(self):
        # OpenCode's button row answers to its option text (the relay maps it to
        # Tab/Enter keystrokes), never to a numeric key press.
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        markup = tg.make_keyboard(
            "w0:p1",
            ["Allow once", "Allow always", "Reject"],
            generation="generation",
            interaction="opencode_permission",
        )
        tg.approval_tokens["w0:p1"] = "generation"
        callback = FakeCallback(markup.inline_keyboard[1][0].callback_data)
        callback.message.reply_markup = markup

        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay,
            patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys,
        ):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_awaited_once_with("w0:p1", "Allow always", prompt_id="prompt-123")
        send_keys.assert_not_awaited()

    async def test_opencode_question_buttons_send_option_text(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        markup = tg.make_keyboard(
            "w0:p1",
            ["Both (Recommended)", "Table only", "Skip admin UI"],
            generation="generation",
            interaction="opencode_question",
        )
        tg.approval_tokens["w0:p1"] = "generation"
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup

        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay,
            patch.object(tg, "send_keys_to_relay", AsyncMock()) as send_keys,
        ):
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_awaited_once_with(
            "w0:p1", "Both (Recommended)", prompt_id="prompt-123")
        send_keys.assert_not_awaited()

    async def test_opencode_review_button_confirms(self):
        markup = tg.make_keyboard(
            "w0:p1", ["Confirm"], generation="generation", interaction="opencode_review",
        )
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        tg.approval_tokens["w0:p1"] = "generation"
        callback = FakeCallback(markup.inline_keyboard[0][0].callback_data)
        callback.message.reply_markup = markup

        with patch.object(tg, "send_to_relay", AsyncMock()) as send_to_relay:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        send_to_relay.assert_awaited_once_with("w0:p1", "Confirm", prompt_id="prompt-123")

    def test_relay_disconnect_clears_approval_generations(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.approval_tokens["w0:p1"] = "generation"
        tg.blocked_prompt_ids["w0:p1"] = "prompt"

        tg.clear_relay_connection_state()

        self.assertFalse(tg.relay_connected)
        self.assertEqual(tg.agents, [])
        self.assertEqual(tg.approval_tokens, {})
        self.assertEqual(tg.blocked_prompt_ids, {})

    async def test_failed_blocked_notification_preserves_previous_generation(self):
        tg.approval_tokens["w0:p1"] = "previous"
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline"))))

        with self.assertRaisesRegex(OSError, "offline"):
            await tg.notify_blocked(app, "w0:p1", "opencode", "project", "prompt", ["yes", "no"])

        self.assertEqual(tg.approval_tokens["w0:p1"], "previous")

    async def test_blocked_notification_failure_does_not_disconnect_relay(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.approval_tokens["w0:p1"] = "previous"
        app = SimpleNamespace(bot=SimpleNamespace(send_message=AsyncMock(side_effect=OSError("offline"))))

        await tg.notify_blocked_safely(app, {
            "pane_id": "w0:p1",
            "agent": "opencode",
            "project": "project",
            "prompt": "prompt",
            "options": ["yes", "no"],
        })

        self.assertTrue(tg.relay_connected)
        self.assertEqual(tg.agents[0]["pane_id"], "w0:p1")
        self.assertEqual(tg.approval_tokens["w0:p1"], "previous")

    async def test_graceful_relay_close_clears_connection_state(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.approval_tokens["w0:p1"] = "generation"
        calls = 0

        def connect(_url):
            nonlocal calls
            calls += 1
            if calls == 1:
                return FakeRelayConnection([])
            self.assertFalse(tg.relay_connected)
            self.assertEqual(tg.agents, [])
            self.assertEqual(tg.approval_tokens, {})
            raise KeyboardInterrupt

        with patch("websockets.connect", side_effect=connect):
            with self.assertRaises(KeyboardInterrupt):
                await tg.relay_listener(SimpleNamespace())

    async def test_callback_rejects_unauthorized_chat(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        callback = FakeCallback({"action": "select_reply", "pane_id": "w0:p1"})

        await tg.handle_callback(make_update(chat_id=7, callback=callback), SimpleNamespace())

        self.assertEqual(callback.answers, ["Unauthorized"])
        self.assertEqual(callback.message.replies, [])

    def test_pending_registry_is_chat_scoped_and_bounded(self):
        tg.register_pending(42, 10, "local:pane")
        tg.register_pending(43, 10, "other:pane")
        self.assertEqual(tg.pending_pane(42, 10), "local:pane")
        self.assertEqual(tg.pending_pane(43, 10), "other:pane")

        for message_id in range(tg.PENDING_LIMIT + 2):
            tg.register_pending(42, 1000 + message_id, f"pane:{message_id}")
        self.assertEqual(len(tg.pending), tg.PENDING_LIMIT)
        self.assertIsNone(tg.pending_pane(42, 1000))

    async def test_blocked_notification_keeps_approvals_and_adds_interaction(self):
        bot = FakeBot()
        await tg.notify_blocked(
            SimpleNamespace(bot=bot),
            pane_id="w0:p1",
            agent="opencode",
            project="blocked-project",
            prompt="Allow tool?",
            options=["yes, single permission", "trust, always allow", "no (tab to edit)"],
        )

        chat_id, text, kwargs, sent = bot.sent[0]
        rows = kwargs["reply_markup"].inline_keyboard
        self.assertEqual([json.loads(row[0].callback_data)["k"] for row in rows[:3]], ["1", "2", "3"])
        self.assertEqual([button.text for button in rows[-1]], ["👁 Details", "💬 Reply"])
        self.assertNotIn("Use an approval button", text)
        self.assertEqual(tg.pending_pane(chat_id, sent.message_id), "w0:p1")

    async def test_group_prompts_keep_independent_pane_mappings(self):
        tg.relay_connected = True
        tg.agents = make_agents(2)
        first = FakeCallback({"action": "select_reply", "pane_id": "w0:p1"}, chat_type="group", message_id=10)
        second = FakeCallback({"action": "select_reply", "pane_id": "w1:p1"}, chat_type="group", message_id=11)

        with patch.object(tg, "read_pane", AsyncMock(return_value="output")):
            await tg.handle_callback(make_update(chat_type="group", callback=first), SimpleNamespace())
            await tg.handle_callback(make_update(chat_type="group", callback=second), SimpleNamespace())

        first_text, first_kwargs, first_sent = first.message.replies[0]
        second_text, second_kwargs, second_sent = second.message.replies[0]
        self.assertNotIn("reply_markup", first_kwargs)
        self.assertNotIn("reply_markup", second_kwargs)
        self.assertIn("Reply to this message", first_text)
        self.assertEqual(tg.pending_pane(42, first_sent.message_id), "w0:p1")
        self.assertEqual(tg.pending_pane(42, second_sent.message_id), "w1:p1")

        first_reply = FakeMessage(chat_type="group")
        first_reply.reply_to_message = SimpleNamespace(message_id=first_sent.message_id)
        first_reply.text = "first response"
        second_reply = FakeMessage(chat_type="group")
        second_reply.reply_to_message = SimpleNamespace(message_id=second_sent.message_id)
        second_reply.text = "second response"
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(chat_type="group", message=first_reply), SimpleNamespace())
            await tg.handle_text(make_update(chat_type="group", message=second_reply), SimpleNamespace())
        self.assertEqual(
            send_text.await_args_list,
            [unittest.mock.call("w0:p1", "first response"), unittest.mock.call("w1:p1", "second response")],
        )

    async def test_native_notification_reply_routes_to_associated_pane(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "follow up"

        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), SimpleNamespace())

        send_text.assert_awaited_once_with("w0:p1", "follow up")
        self.assertEqual(message.replies[0][0], "Sent")

    async def test_blocked_notification_reply_uses_prompt_response(self):
        tg.relay_connected = True
        tg.agents = make_agents(1, status="blocked")
        tg.blocked_prompt_ids["w0:p1"] = "prompt-123"
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "custom answer"

        with (
            patch.object(tg, "send_to_relay", AsyncMock()) as send_response,
            patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text,
        ):
            await tg.handle_text(make_update(message=message), SimpleNamespace())

        send_response.assert_awaited_once_with(
            "w0:p1", "custom answer", prompt_id="prompt-123"
        )
        send_text.assert_not_awaited()

    async def test_mapped_reply_rejects_disconnected_and_stale_panes(self):
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "follow up"

        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), SimpleNamespace())
            send_text.assert_not_awaited()
        self.assertIn("disconnected", message.replies[0][0].lower())

        tg.relay_connected = True
        message.replies.clear()
        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(message=message), SimpleNamespace())
            send_text.assert_not_awaited()
        self.assertIn("no longer available", message.replies[0][0].lower())

    async def test_unauthorized_native_reply_is_ignored(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        tg.register_pending(7, 77, "w0:p1")
        message = FakeMessage(chat_id=7)
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "not allowed"

        with patch.object(tg, "send_text_to_relay", AsyncMock()) as send_text:
            await tg.handle_text(make_update(chat_id=7, message=message), SimpleNamespace())

        send_text.assert_not_awaited()
        self.assertEqual(message.replies, [])

    async def test_duplicate_cross_host_pane_identity_fails_closed(self):
        tg.relay_connected = True
        tg.agents = make_agents(2)
        tg.agents[1]["pane_id"] = tg.agents[0]["pane_id"]
        tg.agents[1]["host"] = "remote.example"
        button = tg.build_agent_keyboard("select_reply").inline_keyboard[0][0]
        callback = FakeCallback(button.callback_data)

        with patch.object(tg, "read_pane", AsyncMock()) as read_pane:
            await tg.handle_callback(make_update(callback=callback), SimpleNamespace())

        read_pane.assert_not_awaited()
        self.assertIn("no longer available", callback.message.replies[0][0].lower())

    async def test_oversized_native_reply_is_rejected_before_relay_send(self):
        tg.relay_connected = True
        tg.agents = make_agents(1)
        tg.register_pending(42, 77, "w0:p1")
        message = FakeMessage()
        message.reply_to_message = SimpleNamespace(message_id=77)
        message.text = "x" * 10001

        await tg.handle_text(make_update(message=message), SimpleNamespace())

        self.assertIn("1-10000", message.replies[0][0])


if __name__ == "__main__":
    unittest.main()
