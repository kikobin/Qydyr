"""Telegram bot (aiogram 3): private chat, friends' group chats with a poll, inline mode.

    python -m qydyr_data serve     # API + Mini App + bot in one process (shared answers and state)
    python -m qydyr_data bot       # bot only (talks to QYDYR_API_URL or to the database directly)

In groups the bot keeps Telegram's privacy mode: it sees /go, replies to its own messages and poll
votes — not the rest of the conversation.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, CallbackQuery,
                           ErrorEvent, FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery,
                           InlineQueryResultArticle, InlineQueryResultsButton, InputTextMessageContent, KeyboardButton,
                           LinkPreviewOptions, MenuButtonWebApp, Message, PollAnswer, ReplyKeyboardMarkup, WebAppInfo,
                           ChatMemberUpdated)

from .. import settings
from ..assistant import travel
from .backend import Backend, make_backend
from .fmt import (answer_html, answer_keyboard, chip_keyboard, hm, in_astana, item_html, markup, meters, poll_options,
                  route_keyboard, stop_html, tour_html, walk_route_url, walk_text, why_not_html)
from .state import Member, State, Walk
from .texts import T, t, user_lang

log = logging.getLogger("qydyr.bot")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
SURPRISE_WORDS = {T[lang]["surprise"].lower() for lang in T}
SURPRISE_TEXT = "удиви меня"
MASCOT_DIR = settings.WEB_DIR / "assets" / "mascot"
PROGRESS_EVERY_S = 240     # live location: "next stop in N m" at most this often (arrivals are always told)
MAX_TEXT, MAX_MEMBERS, MAX_MEMBER = 1000, 12, 300     # what /ask accepts
# a gathering reply that says where the group is ("мы у Хан Шатыра", "I'm at Mega"), not only what it wants
PLACE_HINT = re.compile(r"\b(я|мы|i'?m|i am|we'?re|we are|мен|біз|у|возле|около|напротив|near|at)\b|жанында|қасында|маңында",
                        re.I)


class QydyrBot:
    def __init__(self, backend: Backend | None = None, *, bot_username: str = "", webapp_url: str | None = None) -> None:
        self.backend = backend or make_backend()
        self.state = State()
        self.bot_username = bot_username
        self.webapp_url = webapp_url if webapp_url and webapp_url.startswith("https://") else None
        self.router = Router(name="qydyr")
        self.walk_interval_s = 8.0           # live location ticks every few seconds; arrivals need far fewer
        self._tours: dict[tuple[str, str], dict] = {}
        self._register()

    async def call(self, fn, *args, **kw):
        return await asyncio.to_thread(fn, *args, **kw)

    @staticmethod
    async def edit(coro) -> None:
        """An edit that changes nothing ("message is not modified": a double tap) is fine."""
        try:
            await coro
        except TelegramBadRequest as e:
            if "not modified" not in str(e):
                raise

    def sess(self, u):
        return self.state.session(u.id, u.language_code)

    def lang_for(self, u, chat) -> str:
        """A group speaks the group's language (/lang there, else the starter's); a private chat the person's."""
        if chat is not None and chat.type in GROUPS:
            if chat.id in self.state.chat_lang:
                return self.state.chat_lang[chat.id]
            g = self.state.current(chat.id)
            return g.lang if g else user_lang(u.language_code if u else None)
        return self.sess(u).lang if u else "ru"

    # ----------------------------------------------------------- keyboards
    def reply_kb(self, lang: str) -> ReplyKeyboardMarkup:
        rows = [[KeyboardButton(text=t(lang, "send_location"), request_location=True),
                 KeyboardButton(text=t(lang, "surprise"))]]
        if self.webapp_url:
            rows.append([KeyboardButton(text=t(lang, "map"), web_app=WebAppInfo(url=self.webapp_url))])
        return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)

    def kb(self, ans: dict, lang: str, private: bool = True) -> InlineKeyboardMarkup:
        return markup(answer_keyboard(ans, lang, private=private, webapp_url=self.webapp_url,
                                      bot_username=self.bot_username or None))

    def group_kb(self, gid: str, n: int, lang: str) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text=t(lang, "group_join", n=n), callback_data=f"g:in:{gid}"),
            InlineKeyboardButton(text=t(lang, "group_pick"), callback_data=f"g:pick:{gid}")]])

    # ------------------------------------------------------------- answers
    async def send_answer(self, m: Message, text: str, lang: str, *, lat=None, lon=None, surprise=False,
                          private: bool = True, gps_age: int | None = None) -> dict | None:
        await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        body = {"text": text[:MAX_TEXT], "lang": lang, "format": "structured", "surprise": surprise}
        if lat is not None:
            body.update(lat=lat, lon=lon)
        try:
            res = await self.call(self.backend.ask, **body)
        except Exception as e:  # never leave the person without an answer
            log.exception("ask failed: %s", e)
            await m.answer(t(lang, "error"))
            return None
        ans = res["answer"]
        if ans.get("anchor") is None:
            return ans
        await m.answer(answer_html(ans, lang, gps_age=gps_age), reply_markup=self.kb(ans, lang, private),
                       link_preview_options=NO_PREVIEW)
        return ans

    async def ask_private(self, m: Message, text: str, *, surprise: bool = False) -> None:
        s = self.sess(m.from_user)
        s.pending = None                 # a new request replaces the one that waited for a location
        loc = s.location()
        ans = await self.send_answer(m, text, s.lang, lat=loc[0] if loc else None, lon=loc[1] if loc else None,
                                     surprise=surprise, gps_age=s.loc_age_min() if loc else None)
        if ans is not None and ans.get("anchor") is None:
            s.set_pending(text)
            await m.answer(t(s.lang, "need_place"), reply_markup=self.reply_kb(s.lang))

    # ------------------------------------------------------------ handlers
    def _register(self) -> None:
        r = self.router
        private = F.chat.type == ChatType.PRIVATE
        group = F.chat.type.in_(GROUPS)

        @r.message(CommandStart(), private)
        async def start(m: Message, command: CommandObject) -> None:
            s = self.sess(m.from_user)
            if not s.lang_set:
                s.lang = user_lang(m.from_user.language_code)
            s.clear_location()           # a fresh start: no point from half an hour ago
            s.pending = None
            await self.sticker(m.bot, m.chat.id, "hello")
            await m.answer(t(s.lang, "hello"), reply_markup=self.reply_kb(s.lang))
            arg = (command.args or "").strip()
            if arg.startswith("t_"):
                await self.send_tour(m, arg[2:], s.lang, s)
            elif arg.startswith("a_"):
                ans = await self.call(self.backend.answer, arg[2:])
                if ans:
                    await m.answer(answer_html(ans, s.lang), reply_markup=self.kb(ans, s.lang), link_preview_options=NO_PREVIEW)
                else:
                    await m.answer(t(s.lang, "expired"))

        @r.message(Command("help"))
        async def help_(m: Message) -> None:
            if m.chat.type in GROUPS:
                await m.answer(t(self.lang_for(m.from_user, m.chat), "group_hello"))
                return
            await m.answer(t(self.sess(m.from_user).lang, "help"))

        @r.message(Command("lang"))
        async def lang(m: Message) -> None:
            await m.answer("Тіл / Язык / Language", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Қазақша", callback_data="l:kk"),
                InlineKeyboardButton(text="Русский", callback_data="l:ru"),
                InlineKeyboardButton(text="English", callback_data="l:en")]]))

        @r.message(Command("map"), private)
        async def open_map(m: Message) -> None:
            s = self.sess(m.from_user)
            if not self.webapp_url:
                await m.answer(t(s.lang, "map_off"))
                return
            await m.answer(t(s.lang, "map"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t(s.lang, "map"), web_app=WebAppInfo(url=self.webapp_url))]]))

        @r.message(Command("surprise"), private)
        async def surprise_cmd(m: Message) -> None:
            await self.surprise(m)

        @r.message(Command("walks"), private)
        async def walks(m: Message) -> None:
            s = self.sess(m.from_user)
            await self.send_tours(m, s.lang)
            loc = s.location()
            if not loc:
                s.set_pending("/walks")
                await m.answer(t(s.lang, "need_place"), reply_markup=self.reply_kb(s.lang))
                return
            await self.send_walks(m, s, loc)

        @r.message(F.location, private)
        async def location(m: Message) -> None:
            s = self.sess(m.from_user)
            lat, lon = m.location.latitude, m.location.longitude
            if in_astana(lat, lon):
                s.set_location(lat, lon)
            if s.walk is not None:
                await self.walk_location(m, force=True)
                return
            if not in_astana(lat, lon):  # not "try a taxi": the city itself is the limit
                await m.answer(t(s.lang, "outside"), reply_markup=self.reply_kb(s.lang))
                return
            pending = s.take_pending()
            if pending == "/walks":
                await self.send_walks(m, s, s.location())
                return
            await self.send_answer(m, pending or "что интересного рядом прямо сейчас", s.lang, lat=s.lat, lon=s.lon,
                                   surprise=pending == SURPRISE_TEXT, gps_age=0)

        @r.edited_message(F.location, private)
        async def live_location(m: Message) -> None:
            if self.sess(m.from_user).walk is not None:
                await self.walk_location(m)

        @r.message(F.photo, private)
        async def photo(m: Message) -> None:
            s = self.sess(m.from_user)
            if not settings.ANTHROPIC_API_KEY and not settings.API_URL:
                await m.answer(t(s.lang, "photo_off"))
                return
            await m.answer(t(s.lang, "photo_thinking"))
            buf = io.BytesIO()
            await m.bot.download(m.photo[-1], destination=buf)
            try:
                res = await self.call(self.backend.whereami, buf.getvalue(), s.lang)
            except Exception as e:
                log.warning("whereami: %s", e)
                res = {"found": False}
            if not res.get("found"):
                await m.answer(t(s.lang, "photo_fail"), reply_markup=self.reply_kb(s.lang))
                return
            c = res["landmark"]["confidence"]
            conf = t(s.lang, f"photo_conf_{c if c in ('high', 'medium') else 'low'}")
            await m.answer(t(s.lang, "photo_found", name=escape(res["place"]), conf=conf))
            ans = res["answer"]
            await m.answer(answer_html(ans, s.lang), reply_markup=self.kb(ans, s.lang), link_preview_options=NO_PREVIEW)

        @r.message(F.voice | F.video_note | F.audio, private)
        async def voice(m: Message) -> None:
            await m.answer(t(self.sess(m.from_user).lang, "voice_off"))

        # ------------------------------------------------ group chats
        @r.message(Command("go"), group)
        async def go(m: Message, command: CommandObject) -> None:
            await self.start_gathering(m, (command.args or "").strip())

        @r.message(Command("surprise", "walks", "map"), group)
        async def private_only(m: Message) -> None:
            link = f": t.me/{self.bot_username}" if self.bot_username else ""
            await m.answer(t(self.lang_for(m.from_user, m.chat), "private_only", link=link), link_preview_options=NO_PREVIEW)

        @r.message(CommandStart(), group)
        async def start_in_group(m: Message, command: CommandObject) -> None:
            # t.me/<bot>?startgroup=go: added to a friends' chat from the Mini App — start collecting at once
            await self.start_gathering(m, "" if (command.args or "") == "go" else (command.args or "").strip())

        @r.my_chat_member(group)
        async def added_to_group(u: ChatMemberUpdated) -> None:
            # a plain "Add to group" (no startgroup link): say hello once and show the one command that matters
            if u.new_chat_member.status in ("member", "administrator") and u.old_chat_member.status in ("left", "kicked"):
                lang = self.state.chat_lang.get(u.chat.id) or user_lang(u.from_user.language_code)
                await u.bot.send_message(u.chat.id, t(lang, "group_hello"))

        @r.message(group, F.reply_to_message)
        async def group_reply(m: Message) -> None:
            g = self.state.for_reply(m.chat.id, m.reply_to_message.message_id)
            if g is None:
                return
            new = m.from_user.id not in g.members
            mem = g.members.setdefault(m.from_user.id, Member(name=m.from_user.first_name or t(g.lang, "member")))
            if m.location:
                mem.lat, mem.lon = round(m.location.latitude, 3), round(m.location.longitude, 3)
            elif m.text:
                mem.wish = m.text.strip()[:140]
            elif not new:                # a sticker from someone already counted changes nothing
                return
            await self.refresh_gathering(m.bot, g)

        @r.message(group, F.text)
        async def group_mention(m: Message) -> None:
            # only delivered when privacy mode is off; with privacy on, /go is the way
            if self.bot_username and f"@{self.bot_username.lower()}" in m.text.lower():
                text = re.sub(rf"@{re.escape(self.bot_username)}\b", "", m.text, flags=re.I)
                await self.start_gathering(m, " ".join(text.split()))

        @r.message(F.text, private)
        async def text(m: Message) -> None:
            s = self.sess(m.from_user)
            txt = m.text.strip()
            if txt.startswith("/"):      # every known command has its own handler above
                cmd = txt.split()[0].split("@")[0].lower()
                await m.answer(t(s.lang, "group_hello" if cmd == "/go" else "unknown_cmd"))
                return
            if txt.lower() in SURPRISE_WORDS:
                await self.surprise(m)
                return
            if s.peek_pending() == "/walks":   # "/walks" -> "Где вы?" -> "я у Байтерека": walks from there
                await self.walks_for_text(m, s, txt)
                return
            await self.ask_private(m, m.text)

        @r.message(private)
        async def other(m: Message) -> None:
            s = self.sess(m.from_user)
            await m.answer(t(s.lang, "not_text"), reply_markup=self.reply_kb(s.lang))

        # ------------------------------------------------ callbacks
        @r.callback_query(F.data.startswith("l:"))
        async def set_lang(c: CallbackQuery) -> None:
            lang = c.data[2:] if c.data[2:] in T else "ru"
            await c.answer(t(lang, "lang_set"))
            chat = c.message.chat if c.message else None
            if chat is not None and chat.type in GROUPS:   # the group's language, not the person's
                self.state.chat_lang[chat.id] = lang
                g = self.state.current(chat.id)
                if g is not None:
                    g.lang = lang
                    await self.refresh_gathering(c.bot, g)
                return
            s = self.sess(c.from_user)
            s.lang, s.lang_set = lang, True
            if c.message:
                await c.message.answer(t(lang, "hello"), reply_markup=self.reply_kb(lang))

        @r.callback_query(F.data.startswith("m:"))
        async def route(c: CallbackQuery) -> None:
            _, aid, i = c.data.split(":")
            lang = self.lang_for(c.from_user, c.message.chat if c.message else None)
            ans = await self.call(self.backend.answer, aid)
            if not ans:
                await c.answer(t(lang, "expired"), show_alert=True)
                return
            await c.answer()
            it = ans["items"][int(i)]
            await c.message.answer_venue(latitude=it["lat"], longitude=it["lon"], title=it["title"][:100],
                                         address=f"{walk_text(it, lang)} · {it['status']}"[:200],
                                         reply_markup=markup(route_keyboard(it["lat"], it["lon"], lang)))

        @r.callback_query(F.data.startswith("w:"))
        async def why_not(c: CallbackQuery) -> None:
            ans = await self.call(self.backend.answer, c.data[2:])
            lang = self.lang_for(c.from_user, c.message.chat if c.message else None)
            if not ans:
                await c.answer(t(lang, "expired"), show_alert=True)
                return
            await c.answer()
            await c.message.reply(why_not_html(ans, lang))

        @r.callback_query(F.data.startswith("c:"))
        async def chip(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            aid, ci = parts[1], int(parts[2])
            private_chat = c.message.chat.type == ChatType.PRIVATE
            lang = self.lang_for(c.from_user, c.message.chat)
            ans = await self.call(self.backend.answer, aid)
            if not ans:
                await c.answer(t(lang, "expired"), show_alert=True)
                return
            chip_ = ans["chips"][ci]
            if len(parts) == 3:
                rows = chip_keyboard(ans, ci, lang)
                if rows:
                    await c.answer()
                    await self.edit(c.message.edit_reply_markup(reply_markup=markup(rows)))
                    return
                patch = chip_["options"][0]["patch"]   # a switch: "+ с ребёнком" / "✕ с ребёнком"
            else:
                patch = chip_["options"][int(parts[3])]["patch"]
            await c.answer(t(lang, "thinking"))
            res = await self.call(self.backend.patch, aid, patch)
            if res is None:
                await c.message.answer(t(lang, "expired"))
                return
            new = res["answer"]
            await self.edit(c.message.edit_text(answer_html(new, lang), reply_markup=self.kb(new, lang, private_chat),
                                                link_preview_options=NO_PREVIEW))

        @r.callback_query(F.data.startswith("k:"))
        async def keyboard_back(c: CallbackQuery) -> None:
            await c.answer()
            ans = await self.call(self.backend.answer, c.data[2:])
            if ans:
                lang = self.lang_for(c.from_user, c.message.chat)
                await self.edit(c.message.edit_reply_markup(
                    reply_markup=self.kb(ans, lang, c.message.chat.type == ChatType.PRIVATE)))

        @r.callback_query(F.data.startswith("f:"))
        async def feedback(c: CallbackQuery) -> None:
            from ..pulse import PULSE
            aid = c.data.split(":")[1]
            if self.state.vote_once(aid, c.from_user.id):   # once per person and answer: taps are not votes
                PULSE.vote(c.data.endswith(":up"))
            await c.answer(t(self.lang_for(c.from_user, c.message.chat if c.message else None), "thanks"))

        @r.callback_query(F.data.startswith("g:"))
        async def group_cb(c: CallbackQuery) -> None:
            _, action, gid = c.data.split(":")
            g = self.state.get(gid)
            if g is None:
                lang = self.lang_for(c.from_user, c.message.chat if c.message else None)
                await c.answer(t(lang, "group_no_gathering"), show_alert=True)
                return
            if action == "in":
                await c.answer()
                g.members.setdefault(c.from_user.id, Member(name=c.from_user.first_name or t(g.lang, "member")))
                await self.refresh_gathering(c.bot, g)
            elif action == "pick":
                if g.picking:            # a double tap: one answer and one poll
                    await c.answer(t(g.lang, "group_busy"))
                    return
                await self.pick(c.bot, g, c)
            elif action == "fin":
                if g.final_done:
                    await c.answer(t(g.lang, "group_final_done"), show_alert=True)
                    return
                g.final_done = bool(g.answer and g.answer["items"])   # before the first await, as with "pick"
                await c.answer()
                await self.finish(c.bot, g)

        @r.callback_query(F.data.startswith("tw:"))
        async def tour_card(c: CallbackQuery) -> None:
            await c.answer()
            s = self.sess(c.from_user)
            await self.send_tour(c.message, c.data[3:], s.lang, s)

        @r.callback_query(F.data.startswith("tl:") | F.data.startswith("tm:"))
        async def tour_start(c: CallbackQuery) -> None:
            kind, tour_id, *rest = c.data.split(":")
            await c.answer()
            await self.start_walk(c, tour_id, live=kind == "tl", force=rest == ["f"])

        @r.callback_query(F.data == "tc")
        async def tour_continue(c: CallbackQuery) -> None:
            s = self.sess(c.from_user)
            await c.answer()
            tour = await self.get_tour(s.walk.tour_id, s.lang, s) if s.walk else None
            if tour is None:
                await c.message.answer(t(s.lang, "walk_none_active"))
                return
            nxt = self.next_stop(tour, s.walk.visited)
            text = t(s.lang, "walk_continue", title=escape(s.walk.title))
            if nxt:
                text += " " + t(s.lang, "stop_next_plain", name=escape(nxt["name"]))
            await c.message.answer(text, reply_markup=self.walk_kb(tour, s, s.lang, None, nxt))

        @r.callback_query(F.data.startswith("ta:"))
        async def tour_here(c: CallbackQuery) -> None:
            _, tour_id, stop_id = c.data.split(":", 2)
            s = self.sess(c.from_user)
            await c.answer()
            if s.walk is None:
                await c.message.answer(t(s.lang, "walk_none_active"))
                return
            if s.walk.tour_id != tour_id:    # a button from an earlier walk
                await c.message.answer(t(s.lang, "walk_other", title=escape(s.walk.title)))
                return
            tour = await self.get_tour(tour_id, s.lang, s)
            if tour:
                await self.walk_arrive(c.bot, c.message.chat.id, s, tour, stop_id)

        @r.callback_query(F.data.startswith("ts:"))
        async def tour_story(c: CallbackQuery) -> None:
            _, tour_id, stop_id = c.data.split(":", 2)
            lang = self.sess(c.from_user).lang
            await c.answer(t(lang, "thinking"))
            story = await self.call(self.backend.tour_story, tour_id, stop_id, lang)
            if story:
                await c.message.answer(t(lang, "walk_story", text=escape(story["text"])), link_preview_options=NO_PREVIEW)

        @r.callback_query(F.data.startswith("te:"))
        async def tour_end(c: CallbackQuery) -> None:
            s = self.sess(c.from_user)
            await c.answer()
            if s.walk is None:
                await c.message.answer(t(s.lang, "walk_none_active"))
                return
            await self.walk_finish(c.bot, c.message.chat.id, s)

        @r.poll_answer()
        async def poll_answer(p: PollAnswer) -> None:
            gid = self.state.by_poll.get(p.poll_id)
            g = self.state.get(gid) if gid else None
            if g is None or p.user is None or p.poll_id != g.poll_id:
                return
            if p.option_ids:
                g.votes[p.user.id] = p.option_ids[0]
            else:
                g.votes.pop(p.user.id, None)

        # ------------------------------------------------ inline mode
        @r.inline_query()
        async def inline(q: InlineQuery) -> None:
            s = self.state.sessions.get(q.from_user.id)
            lang = s.lang if s else user_lang(q.from_user.language_code)
            text = q.query.strip()
            link = f": t.me/{self.bot_username}" if self.bot_username else ""
            # picked by mistake, the hint sends one line about the bot, not the private chat's greeting
            share = InputTextMessageContent(message_text=t(lang, "inline_share", link=link), link_preview_options=NO_PREVIEW)
            hint = InlineQueryResultArticle(id="hint", title=t(lang, "inline_hint"), input_message_content=share)
            button = InlineQueryResultsButton(text=t(lang, "inline_button"), start_parameter="inline")
            if len(text) < 3:
                await q.answer([hint], cache_time=60, is_personal=True, button=button)
                return
            try:
                res = await self.call(self.backend.ask, text=text, lang=lang, format="structured", llm=False)
            except Exception:
                await q.answer([hint], cache_time=10, is_personal=True, button=button)
                return
            ans = res["answer"]
            if not ans.get("anchor"):
                await q.answer([hint], cache_time=10, is_personal=True, button=button)
                return
            out = [InlineQueryResultArticle(
                id=f"{ans['id']}-{i}", title=f"{i + 1}. {it['title']}",
                description=f"{walk_text(it, lang)} · {it['status']}"[:200],
                input_message_content=InputTextMessageContent(message_text=item_html(ans, i, lang), parse_mode=ParseMode.HTML,
                                                              link_preview_options=NO_PREVIEW),
                reply_markup=markup(route_keyboard(it["lat"], it["lon"], lang)))
                for i, it in enumerate(ans["items"])]
            await q.answer(out or [InlineQueryResultArticle(
                id="none", title=t(lang, "inline_none"),
                input_message_content=InputTextMessageContent(message_text=t(lang, "inline_none")))],
                cache_time=30, is_personal=True, button=button)

        # ------------------------------------------------ errors
        @r.errors()
        async def on_error(ev: ErrorEvent, bot: Bot) -> bool:
            """aiogram only logs a failed handler; the person would see a spinning button or nothing at all."""
            exc, u = ev.exception, ev.update
            if isinstance(exc, TelegramBadRequest) and "not modified" in str(exc):
                return True
            log.error("update %s failed: %r", u.update_id, exc, exc_info=exc)
            try:
                if u.callback_query is not None:
                    cq = u.callback_query
                    chat = cq.message.chat if cq.message else None
                    lang = self.lang_for(cq.from_user, chat)
                    try:
                        await cq.answer(t(lang, "error"), show_alert=True)
                    except TelegramBadRequest:   # answered already: say it in the chat
                        if chat is not None:
                            await bot.send_message(chat.id, t(lang, "error"))
                elif u.message is not None and (u.message.chat.type == ChatType.PRIVATE
                                                or (u.message.text or "").startswith("/")):
                    await bot.send_message(u.message.chat.id, t(self.lang_for(u.message.from_user, u.message.chat), "error"))
            except Exception as e:
                log.warning("could not report the error: %s", e)
            return True

    # ------------------------------------------------------------- flows
    async def surprise(self, m: Message) -> None:
        await self.ask_private(m, SURPRISE_TEXT, surprise=True)

    async def walks_for_text(self, m: Message, s, text: str) -> None:
        """/walks without a location, then a typed place: the walks around that place."""
        s.pending = None
        await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        res = await self.call(self.backend.ask, text=text[:MAX_TEXT], lang=s.lang, format="structured", llm=False)
        a = res["answer"].get("anchor")
        if not a:
            s.set_pending("/walks")
            await m.answer(t(s.lang, "need_place"), reply_markup=self.reply_kb(s.lang))
            return
        await self.send_walks(m, s, (a["lat"], a["lon"]))

    async def send_walks(self, m: Message, s, loc) -> None:
        """Themed walks around a point. Each can be walked with the guide (as a tour kept in the session) or
        opened as one walking route in Google Maps."""
        lang = s.lang
        await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        walks = await self.call(self.backend.walks, loc[0], loc[1], lang)
        if not walks:
            await m.answer(t(lang, "walks_none"))
            return
        s.gen_tours = {w["tour"]["id"]: w["tour"] for w in walks if (w.get("tour") or {}).get("stops_list")}
        lines, rows = [t(lang, "walks_title")], []
        for w in walks:
            stops = " → ".join(x["title"] for x in (w["answer"].get("route") or {}).get("stops", [])) or \
                " · ".join(i["title"] for i in w["answer"]["items"])
            lines.append(f"\n<b>{escape(w['title'])}</b> · {escape(w['meta'])}\n{escape(stops)}")
            tour, row = w.get("tour") or {}, []
            if tour.get("id") in s.gen_tours:
                row.append(InlineKeyboardButton(text=w["title"][:60], callback_data=f"tw:{tour['id']}"))
                url = walk_route_url(tour["stops_list"])
                if url:
                    row.append(InlineKeyboardButton(text=t(lang, "route_one"), url=url))
            if row:
                rows.append(row)
        if rows:
            lines.append("\n" + t(lang, "walks_hint"))
        if self.webapp_url:
            rows.append([InlineKeyboardButton(text=t(lang, "map"), web_app=WebAppInfo(url=self.webapp_url))])
        await m.answer("\n".join(lines), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows) if rows else None,
                       link_preview_options=NO_PREVIEW)

    # ------------------------------------------------------- guided walks
    async def sticker(self, bot: Bot, chat_id: int, mood: str) -> None:
        """Суырбек as a sticker (static WEBP from assets/mascot) — a small touch, never a failure."""
        path = MASCOT_DIR / f"{mood}.webp"
        if not path.exists():
            return
        try:
            await bot.send_sticker(chat_id, FSInputFile(path))
        except Exception as e:
            log.debug("sticker %s: %s", mood, e)

    async def get_tour(self, tour_id: str, lang: str, s=None) -> dict | None:
        if s is not None and tour_id in s.gen_tours:   # a themed walk from this person's /walks
            return s.gen_tours[tour_id]
        if tour_id.startswith("walk_"):
            return None
        key = (tour_id, lang)
        if key not in self._tours:
            tour = await self.call(self.backend.tour, tour_id, lang)
            if tour is None:
                return None
            self._tours[key] = tour
        return self._tours[key]

    def tour_url(self, tour_id: str) -> str | None:
        """The tour in the Mini App; themed walks are not there, and without HTTPS there is no Mini App."""
        return f"{self.webapp_url}?tour={tour_id}" if self.webapp_url and not tour_id.startswith("walk_") else None

    async def send_tours(self, m: Message, lang: str) -> None:
        try:
            tours = await self.call(self.backend.tours, lang)
        except Exception as e:
            log.warning("tours: %s", e)
            tours = []
        if not tours:
            return
        rows = [[InlineKeyboardButton(text=f"{c['title']} · {c['meta']}"[:64], callback_data=f"tw:{c['id']}")] for c in tours]
        await m.answer(t(lang, "tours_title"), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def send_tour(self, m: Message, tour_id: str, lang: str, s=None) -> None:
        tour = await self.get_tour(tour_id, lang, s)
        if tour is None:
            await m.answer(t(lang, "expired" if tour_id.startswith("walk_") else "tours_none"))
            return
        rows = [[InlineKeyboardButton(text=t(lang, "btn_live"), callback_data=f"tl:{tour_id}"),
                 InlineKeyboardButton(text=t(lang, "btn_manual"), callback_data=f"tm:{tour_id}")]]
        url = self.tour_url(tour_id)
        if url and m.chat.type == ChatType.PRIVATE:
            rows.append([InlineKeyboardButton(text=t(lang, "btn_tour_map"), web_app=WebAppInfo(url=url))])
        elif url and self.bot_username:
            rows.append([InlineKeyboardButton(text=t(lang, "btn_tour_map"),
                                              url=f"https://t.me/{self.bot_username}?startapp=t_{tour_id}")])
        await m.answer(tour_html(tour), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), link_preview_options=NO_PREVIEW)

    def walk_kb(self, tour: dict, s, lang: str, stop: dict | None, nxt: dict | None) -> InlineKeyboardMarkup:
        rows = []
        if nxt is not None:
            name = nxt["name"] if len(nxt["name"]) <= 40 else nxt["name"][:38] + "…"
            rows.append([InlineKeyboardButton(text=t(lang, "btn_here", name=name), callback_data=f"ta:{tour['id']}:{nxt['id']}")])
        last = []
        if stop is not None and tour.get("kind") != "walk" and (settings.ANTHROPIC_API_KEY or settings.API_URL):
            last.append(InlineKeyboardButton(text=t(lang, "btn_more"), callback_data=f"ts:{tour['id']}:{stop['id']}"))
        last.append(InlineKeyboardButton(text=t(lang, "btn_end"), callback_data=f"te:{tour['id']}"))
        rows.append(last)
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def next_stop(tour: dict, visited: list[str]) -> dict | None:
        stops = tour["stops_list"]
        reached = [i for i, x in enumerate(stops) if x["id"] in visited]
        start = max(reached) + 1 if reached else 0
        return next((x for x in stops[start:] + stops[:start] if x["id"] not in visited), None)

    async def start_walk(self, c: CallbackQuery, tour_id: str, live: bool, force: bool = False) -> None:
        s = self.sess(c.from_user)
        tour = await self.get_tour(tour_id, s.lang, s)
        if tour is None:
            await c.message.answer(t(s.lang, "expired" if tour_id.startswith("walk_") else "tours_none"))
            return
        if s.walk is not None and not force:   # never drop a walk in progress silently
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=t(s.lang, "btn_replace"), callback_data=f"{'tl' if live else 'tm'}:{tour_id}:f")],
                [InlineKeyboardButton(text=t(s.lang, "btn_continue"), callback_data="tc")]])
            await c.message.answer(t(s.lang, "walk_replace", title=escape(s.walk.title), new=escape(tour["title"])),
                                   reply_markup=kb)
            return
        s.walk = Walk(tour_id=tour_id, title=tour["title"], total=len(tour["stops_list"]), live=live)
        s.pending = None
        first = tour["stops_list"][0]
        await self.sticker(c.bot, c.message.chat.id, "happy")
        text = t(s.lang, "tour_start_live" if live else "tour_start_manual", first=escape(first["name"]))
        if live:
            text += "\n\n" + t(s.lang, "tour_privacy")
        await c.message.answer(text, reply_markup=self.walk_kb(tour, s, s.lang, None, first), link_preview_options=NO_PREVIEW)

    async def progress(self, tour: dict, w: Walk, lat: float, lon: float, accuracy: float | None = None) -> dict:
        if tour.get("kind") != "walk":
            return await self.call(self.backend.tour_progress, w.tour_id, lat, lon, list(w.visited), accuracy)
        # a themed walk from /walks is not in tours.yaml: arrival and the next stop are worked out here
        slack = 40 + min(max(accuracy or 0, 0), 60)
        near = [(travel.haversine_m(lat, lon, x["lat"], x["lon"]), x) for x in tour["stops_list"] if x["id"] not in w.visited]
        near = [p for p in near if p[0] <= p[1].get("radius_m", 70) + slack]
        arrived = min(near, key=lambda p: p[0])[1]["id"] if near else None
        nxt = self.next_stop(tour, list(w.visited) + ([arrived] if arrived else []))
        return {"arrived": arrived, "next": nxt["id"] if nxt else None, "off_route": False,
                "next_m": round(travel.street_m(lat, lon, nxt["lat"], nxt["lon"])) if nxt else None,
                "next_min": travel.walk_min(lat, lon, nxt["lat"], nxt["lon"]) if nxt else None}

    async def walk_location(self, m: Message, force: bool = False) -> None:
        """One location (a sent point or a live-location tick): did we reach a stop? Used in memory only."""
        s = self.sess(m.from_user)
        w = s.walk
        now = time.time()
        if w is None or (not force and now - w.last_update < self.walk_interval_s):
            return
        w.last_update = now
        lat, lon = m.location.latitude, m.location.longitude
        tour = await self.get_tour(w.tour_id, s.lang, s)
        if tour is None:
            return
        pr = await self.progress(tour, w, lat, lon, m.location.horizontal_accuracy)
        if pr["arrived"]:
            await self.walk_arrive(m.bot, m.chat.id, s, tour, pr["arrived"], next_m=pr["next_m"], next_min=pr["next_min"])
            return
        nxt = next((x for x in tour["stops_list"] if x["id"] == pr["next"]), None)
        if pr["off_route"] and nxt and now - w.last_off_route > 300:
            w.last_off_route = w.last_progress = now
            await m.answer(t(s.lang, "walk_off_route", name=escape(nxt["name"]), dist=meters(pr["next_m"], s.lang)))
        elif nxt and pr["next_m"] is not None and (force or now - w.last_progress > PROGRESS_EVERY_S):
            # a sent point or the first live tick is always answered: the person sees the bot follows them
            w.last_progress = now
            await m.answer(t(s.lang, "walk_progress", name=escape(nxt["name"]), dist=meters(pr["next_m"], s.lang),
                             min=pr["next_min"]))
        if now - w.last_nudge_check > 180:
            w.last_nudge_check = now
            try:
                res = await self.call(self.backend.walk_nudges, round(lat, 3), round(lon, 3),
                                      nxt["lat"] if nxt else None, nxt["lon"] if nxt else None, s.lang)
            except Exception as e:
                log.debug("nudges: %s", e)
                return
            for n in res.get("nudges", []):
                key = f"{n['kind']}:{n.get('event_id') or n.get('place_id') or n['text']}"
                if key not in w.nudged:          # once per event or place, not once per distance
                    w.nudged.add(key)
                    await m.answer(escape(n["text"]))

    async def walk_arrive(self, bot: Bot, chat_id: int, s, tour: dict, stop_id: str, next_m: int | None = None,
                          next_min: int | None = None) -> None:
        w = s.walk
        stop = next((x for x in tour["stops_list"] if x["id"] == stop_id), None)
        if w is None or stop is None or stop_id in w.visited:
            return
        w.visited.append(stop_id)
        w.facts += len(stop["facts"])
        nxt = self.next_stop(tour, w.visited)
        text = stop_html(tour, stop, s.lang, nxt, next_m if nxt else None, next_min)
        await bot.send_message(chat_id, text, reply_markup=self.walk_kb(tour, s, s.lang, stop, nxt),
                               link_preview_options=NO_PREVIEW)
        if nxt is None:
            await self.walk_finish(bot, chat_id, s)

    async def walk_finish(self, bot: Bot, chat_id: int, s) -> None:
        w, s.walk = s.walk, None
        if w is None:
            return
        if not w.visited:                # stopped before the first stop: nothing to celebrate
            await bot.send_message(chat_id, t(s.lang, "walk_stopped", title=escape(w.title)))
            return
        await self.sticker(bot, chat_id, "celebrate")
        await bot.send_message(chat_id, t(s.lang, "walk_done", title=escape(w.title), stops=len(w.visited), total=w.total,
                                          facts=w.facts))

    # ------------------------------------------------------- group chats
    async def start_gathering(self, m: Message, text: str) -> None:
        lang = self.state.chat_lang.get(m.chat.id) or user_lang(m.from_user.language_code)
        g = self.state.new_gathering(m.chat.id, lang, text)
        g.starter = m.from_user.id
        g.members[m.from_user.id] = Member(name=m.from_user.first_name or t(lang, "member"))
        where = f" — {escape(text)}" if text else ""
        msg = await m.answer(t(lang, "group_start", where=where), reply_markup=self.group_kb(g.gid, len(g.members), lang))
        self.state.bind_message(g, msg.message_id)

    async def refresh_gathering(self, bot: Bot, g) -> None:
        where = f" — {escape(g.text)}" if g.text else ""
        lines = [t(g.lang, "group_start", where=where)]
        loc = " · " + t(g.lang, "has_location")
        wishes = [escape(m.name) + (f" — {escape(m.wish)}" if m.wish else "") + (loc if m.lat is not None else "")
                  for m in g.members.values() if m.wish or m.lat is not None]
        if wishes:
            lines.append("")
            lines.append(f"<b>{t(g.lang, 'group_members')}</b>")
            lines += wishes
        try:
            await bot.edit_message_text("\n".join(lines), chat_id=g.chat_id, message_id=g.message_id,
                                        reply_markup=self.group_kb(g.gid, len(g.members), g.lang))
        except Exception as e:  # "message is not modified" and the like
            log.debug("refresh gathering: %s", e)

    @staticmethod
    def group_request(g) -> tuple[list[str], dict]:
        """What to ask for the group. The /go text is the starter's place and wish; who said nothing goes along
        with it. If /go named no place, a reply that does ("мы у Хан Шатыра") is where to look."""
        members = [f"{mem.name}: {mem.wish or g.text}"[:MAX_MEMBER] for mem in g.members.values() if mem.wish or g.text]
        texts = [g.text[:MAX_TEXT]] if g.text else []
        texts += [mem.wish for mem in reversed(list(g.members.values()))
                  if mem.wish and mem.wish != g.text and PLACE_HINT.search(mem.wish)]
        body = {"members": members[:MAX_MEMBERS] or None, "lang": g.lang, "format": "structured"}
        pts = g.points()
        if pts:
            body["lat"] = sum(p[0] for p in pts) / len(pts)
            body["lon"] = sum(p[1] for p in pts) / len(pts)
        return texts or ["куда пойти вместе"], body

    async def pick(self, bot: Bot, g, c: CallbackQuery | None = None) -> None:
        g.picking = True                 # before the first await: a second tap must already see it
        try:
            if c is not None:
                await c.answer(t(g.lang, "thinking"))
            await bot.send_chat_action(g.chat_id, ChatAction.TYPING)
            texts, body = self.group_request(g)
            ans = None
            for text in texts:
                ans = (await self.call(self.backend.ask, text=text, **body))["answer"]
                if ans.get("anchor"):
                    break
            if not ans or not ans.get("anchor"):
                await bot.send_message(g.chat_id, t(g.lang, "group_need_place"))
                return
            self.state.reset_poll(g)     # a new answer: votes for the old one no longer count
            g.answer = ans
            text = answer_html(ans, g.lang)
            pts = g.points()
            if len(pts) >= 2 and ans["items"]:
                worst = [(max(travel.walk_min(p[0], p[1], it["lat"], it["lon"]) for p in pts), i)
                         for i, it in enumerate(ans["items"])]
                mins, best = min(worst)
                text += "\n\n" + t(g.lang, "group_meet", title=escape(ans["items"][best]["title"]), mins=mins)
            await bot.send_message(g.chat_id, text, reply_markup=self.kb(ans, g.lang, private=False),
                                   link_preview_options=NO_PREVIEW)
            if len(ans["items"]) >= 2:
                g.options = list(range(len(ans["items"])))
                poll = await bot.send_poll(g.chat_id, question=t(g.lang, "group_poll"),
                                           options=poll_options(ans["items"], g.lang), is_anonymous=False,
                                           reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                               text=t(g.lang, "group_final"), callback_data=f"g:fin:{g.gid}")]]))
                self.state.bind_poll(g, poll.poll.id, poll.message_id)
            elif ans["items"]:
                await self.finish(bot, g)
        except Exception as e:
            log.exception("group pick failed: %s", e)
            await bot.send_message(g.chat_id, t(g.lang, "group_error"))
        finally:
            g.picking = False

    async def finish(self, bot: Bot, g) -> None:
        ans = g.answer
        if not ans or not ans["items"]:
            await bot.send_message(g.chat_id, t(g.lang, "group_no_gathering"))
            return
        g.final_done = True
        if g.poll_message_id:            # the vote is over: close the poll and drop its "Итог" button
            try:
                await bot.stop_poll(g.chat_id, g.poll_message_id, reply_markup=InlineKeyboardMarkup(inline_keyboard=[]))
            except Exception as e:
                log.debug("stop poll: %s", e)
        tally = [0] * len(ans["items"])
        for opt in g.votes.values():
            if 0 <= opt < len(tally):
                tally[opt] += 1
        win = max(range(len(tally)), key=lambda i: (tally[i], -i))
        it = ans["items"][win]
        leave = hm(it["leave_by"]) if it.get("leave_by") else hm(ans["at"])
        text = t(g.lang, "group_winner", title=escape(it["title"]),
                 leave=t(g.lang, "group_leave", time=leave, walk=walk_text(it, g.lang)))
        await bot.send_message(g.chat_id, text, reply_markup=markup(route_keyboard(it["lat"], it["lon"], g.lang)))


async def setup_bot(bot: Bot, qb: QydyrBot) -> None:
    me = await bot.get_me()
    qb.bot_username = me.username or ""
    if not settings.BOT_USERNAME:   # the Mini App's "with friends" and deep links read it from /config
        settings.BOT_USERNAME = qb.bot_username
    names = ["start", "walks", "surprise"] + (["map"] if qb.webapp_url else []) + ["lang", "help"]
    for code in (None, "ru", "kk", "en"):   # None: everyone whose Telegram language is not one of ours
        lang = code or "ru"
        await bot.set_my_commands([BotCommand(command=c, description=t(lang, f"cmd_{c}")) for c in names],
                                  scope=BotCommandScopeAllPrivateChats(), language_code=code)
        await bot.set_my_commands([BotCommand(command="go", description=t(lang, "cmd_go")),
                                   BotCommand(command="lang", description=t(lang, "cmd_lang"))],
                                  scope=BotCommandScopeAllGroupChats(), language_code=code)
    if qb.webapp_url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Карта", web_app=WebAppInfo(url=qb.webapp_url)))


async def run_bot(backend: Backend | None = None) -> None:
    if not settings.TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    bot = Bot(settings.TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    webapp = f"{settings.PUBLIC_URL}/app/" if settings.PUBLIC_URL else None
    qb = QydyrBot(backend, webapp_url=webapp)
    await setup_bot(bot, qb)
    dp = Dispatcher()
    dp.include_router(qb.router)
    log.info("bot @%s is polling (Mini App: %s)", qb.bot_username, qb.webapp_url or "no HTTPS URL")
    await dp.start_polling(bot, handle_signals=False)
