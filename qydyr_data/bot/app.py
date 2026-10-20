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
import time
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ChatType, ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats, CallbackQuery,
                           FSInputFile, InlineKeyboardButton, InlineKeyboardMarkup, InlineQuery, InlineQueryResultArticle,
                           InputTextMessageContent, KeyboardButton, LinkPreviewOptions, MenuButtonWebApp, Message,
                           PollAnswer, ReplyKeyboardMarkup, WebAppInfo, ChatMemberUpdated)

from .. import settings
from ..assistant import travel
from .backend import Backend, make_backend
from .fmt import (answer_html, answer_keyboard, chip_keyboard, hm, item_html, markup, meters, route_keyboard, stop_html,
                  tour_html, walk_text, why_not_html)
from .state import Member, State, Walk
from .texts import T, t

log = logging.getLogger("qydyr.bot")
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
GROUPS = {ChatType.GROUP, ChatType.SUPERGROUP}
SURPRISE_WORDS = {T[lang]["surprise"].lower() for lang in T}
MASCOT_DIR = settings.WEB_DIR / "assets" / "mascot"


def user_lang(code: str | None) -> str:
    code = (code or "").lower()
    return "kk" if code.startswith("kk") else "en" if code.startswith("en") else "ru"


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
                          private: bool = True) -> dict | None:
        await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
        body = {"text": text, "lang": lang, "format": "structured", "surprise": surprise}
        if lat is not None:
            body.update(lat=lat, lon=lon)
        try:
            res = await self.call(self.backend.ask, **body)
        except Exception as e:  # never leave the person without an answer
            log.exception("ask failed: %s", e)
            await m.answer("Не получилось подобрать — попробуйте ещё раз через минуту.")
            return None
        ans = res["answer"]
        if ans.get("anchor") is None:
            return ans
        await m.answer(answer_html(ans, lang), reply_markup=self.kb(ans, lang, private), link_preview_options=NO_PREVIEW)
        return ans

    async def ask_private(self, m: Message, text: str) -> None:
        s = self.state.session(m.from_user.id)
        loc = s.location()
        ans = await self.send_answer(m, text, s.lang, lat=loc[0] if loc else None, lon=loc[1] if loc else None)
        if ans is not None and ans.get("anchor") is None:
            s.pending = text
            await m.answer(t(s.lang, "need_place"), reply_markup=self.reply_kb(s.lang))

    # ------------------------------------------------------------ handlers
    def _register(self) -> None:
        r = self.router
        private = F.chat.type == ChatType.PRIVATE
        group = F.chat.type.in_(GROUPS)

        @r.message(CommandStart(), private)
        async def start(m: Message, command: CommandObject) -> None:
            s = self.state.session(m.from_user.id)
            s.lang = user_lang(m.from_user.language_code)
            await self.sticker(m.bot, m.chat.id, "hello")
            await m.answer(t(s.lang, "hello"), reply_markup=self.reply_kb(s.lang))
            arg = (command.args or "").strip()
            if arg.startswith("t_"):
                await self.send_tour(m, arg[2:], s.lang)
                return
            if arg.startswith("a_") and (ans := await self.call(self.backend.answer, arg[2:])):
                await m.answer(answer_html(ans, s.lang), reply_markup=self.kb(ans, s.lang), link_preview_options=NO_PREVIEW)

        @r.message(Command("help"))
        async def help_(m: Message) -> None:
            await m.answer(t(self.state.session(m.from_user.id).lang, "help"))

        @r.message(Command("lang"))
        async def lang(m: Message) -> None:
            await m.answer("Тіл / Язык / Language", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="Қазақша", callback_data="l:kk"),
                InlineKeyboardButton(text="Русский", callback_data="l:ru"),
                InlineKeyboardButton(text="English", callback_data="l:en")]]))

        @r.message(Command("map"), private)
        async def open_map(m: Message) -> None:
            s = self.state.session(m.from_user.id)
            if not self.webapp_url:
                await m.answer("Карта откроется, когда у сервера будет HTTPS-адрес (QYDYR_PUBLIC_URL).")
                return
            await m.answer(t(s.lang, "map"), reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=t(s.lang, "map"), web_app=WebAppInfo(url=self.webapp_url))]]))

        @r.message(Command("surprise"), private)
        async def surprise_cmd(m: Message) -> None:
            await self.surprise(m)

        @r.message(Command("walks"), private)
        async def walks(m: Message) -> None:
            s = self.state.session(m.from_user.id)
            await self.send_tours(m, s.lang)
            loc = s.location()
            if not loc:
                s.pending = "/walks"
                await m.answer(t(s.lang, "need_place"), reply_markup=self.reply_kb(s.lang))
                return
            await self.send_walks(m, s.lang, loc)

        @r.message(F.location, private)
        async def location(m: Message) -> None:
            s = self.state.session(m.from_user.id)
            s.set_location(m.location.latitude, m.location.longitude)
            if s.walk is not None:
                await self.walk_location(m, force=True)
                return
            pending, s.pending = s.pending, None
            if pending == "/walks":
                await self.send_walks(m, s.lang, s.location())
                return
            await self.send_answer(m, pending or "что интересного рядом прямо сейчас", s.lang, lat=s.lat, lon=s.lon)

        @r.edited_message(F.location, private)
        async def live_location(m: Message) -> None:
            if self.state.session(m.from_user.id).walk is not None:
                await self.walk_location(m)

        @r.message(F.photo, private)
        async def photo(m: Message) -> None:
            s = self.state.session(m.from_user.id)
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
            conf = {"high": "уверен", "medium": "почти уверен"}.get(res["landmark"]["confidence"], "не уверен")
            await m.answer(t(s.lang, "photo_found", name=res["place"], conf=conf))
            ans = res["answer"]
            await m.answer(answer_html(ans, s.lang), reply_markup=self.kb(ans, s.lang), link_preview_options=NO_PREVIEW)

        @r.message(F.voice | F.video_note | F.audio, private)
        async def voice(m: Message) -> None:
            await m.answer(t(self.state.session(m.from_user.id).lang, "voice_off"))

        # ------------------------------------------------ group chats
        @r.message(Command("go"), group)
        async def go(m: Message, command: CommandObject) -> None:
            await self.start_gathering(m, (command.args or "").strip())

        @r.message(CommandStart(), group)
        async def start_in_group(m: Message, command: CommandObject) -> None:
            # t.me/<bot>?startgroup=go: added to a friends' chat from the Mini App — start collecting at once
            await self.start_gathering(m, "" if (command.args or "") == "go" else (command.args or "").strip())

        @r.my_chat_member(group)
        async def added_to_group(u: ChatMemberUpdated) -> None:
            # a plain "Add to group" (no startgroup link): say hello once and show the one command that matters
            if u.new_chat_member.status in ("member", "administrator") and u.old_chat_member.status in ("left", "kicked"):
                await u.bot.send_message(u.chat.id, t(user_lang(u.from_user.language_code), "group_hello"))

        @r.message(group, F.reply_to_message)
        async def group_reply(m: Message) -> None:
            g = self.state.for_reply(m.chat.id, m.reply_to_message.message_id)
            if g is None:
                return
            mem = g.members.setdefault(m.from_user.id, Member(name=m.from_user.first_name or "Участник"))
            if m.location:
                mem.lat, mem.lon = round(m.location.latitude, 3), round(m.location.longitude, 3)
            elif m.text:
                mem.wish = m.text.strip()[:140]
            await self.refresh_gathering(m.bot, g)

        @r.message(group, F.text)
        async def group_mention(m: Message) -> None:
            # only delivered when privacy mode is off; with privacy on, /go is the way
            if self.bot_username and f"@{self.bot_username.lower()}" in m.text.lower():
                await self.start_gathering(m, m.text.replace(f"@{self.bot_username}", "").strip())

        @r.message(F.text, private)
        async def text(m: Message) -> None:
            if m.text.strip().lower() in SURPRISE_WORDS:
                await self.surprise(m)
                return
            await self.ask_private(m, m.text)

        # ------------------------------------------------ callbacks
        @r.callback_query(F.data.startswith("l:"))
        async def set_lang(c: CallbackQuery) -> None:
            s = self.state.session(c.from_user.id)
            s.lang = c.data[2:]
            await c.answer(t(s.lang, "lang_set"))
            if c.message.chat.type == ChatType.PRIVATE:
                await c.message.answer(t(s.lang, "hello"), reply_markup=self.reply_kb(s.lang))

        @r.callback_query(F.data.startswith("m:"))
        async def route(c: CallbackQuery) -> None:
            _, aid, i = c.data.split(":")
            ans = await self.call(self.backend.answer, aid)
            lang = self.state.session(c.from_user.id).lang
            if not ans:
                await c.answer(t(lang, "expired"), show_alert=True)
                return
            it = ans["items"][int(i)]
            await c.message.answer_venue(latitude=it["lat"], longitude=it["lon"], title=it["title"][:100],
                                         address=f"{walk_text(it)} · {it['status']}"[:200],
                                         reply_markup=markup(route_keyboard(it["lat"], it["lon"], lang)))
            await c.answer()

        @r.callback_query(F.data.startswith("w:"))
        async def why_not(c: CallbackQuery) -> None:
            ans = await self.call(self.backend.answer, c.data[2:])
            lang = self.state.session(c.from_user.id).lang
            if not ans:
                await c.answer(t(lang, "expired"), show_alert=True)
                return
            await c.message.reply(why_not_html(ans, lang))
            await c.answer()

        @r.callback_query(F.data.startswith("c:"))
        async def chip(c: CallbackQuery) -> None:
            parts = c.data.split(":")
            aid, ci = parts[1], int(parts[2])
            lang = self.state.session(c.from_user.id).lang
            ans = await self.call(self.backend.answer, aid)
            if not ans:
                await c.answer(t(lang, "expired"), show_alert=True)
                return
            chip_ = ans["chips"][ci]
            if len(parts) == 3:
                rows = chip_keyboard(ans, ci, lang)
                if rows:
                    await c.message.edit_reply_markup(reply_markup=markup(rows))
                    await c.answer()
                    return
                patch = chip_["options"][0]["patch"]
            else:
                patch = chip_["options"][int(parts[3])]["patch"]
            await c.answer(t(lang, "thinking"))
            res = await self.call(self.backend.patch, aid, patch)
            if res is None:
                await c.message.answer(t(lang, "expired"))
                return
            new = res["answer"]
            private_chat = c.message.chat.type == ChatType.PRIVATE
            await c.message.edit_text(answer_html(new, lang), reply_markup=self.kb(new, lang, private_chat),
                                      link_preview_options=NO_PREVIEW)

        @r.callback_query(F.data.startswith("k:"))
        async def keyboard_back(c: CallbackQuery) -> None:
            ans = await self.call(self.backend.answer, c.data[2:])
            if ans:
                await c.message.edit_reply_markup(reply_markup=self.kb(ans, self.state.session(c.from_user.id).lang,
                                                                       c.message.chat.type == ChatType.PRIVATE))
            await c.answer()

        @r.callback_query(F.data.startswith("f:"))
        async def feedback(c: CallbackQuery) -> None:
            from ..pulse import PULSE
            PULSE.vote(c.data.endswith(":up"))
            await c.answer(t(self.state.session(c.from_user.id).lang, "thanks"))

        @r.callback_query(F.data.startswith("g:"))
        async def group_cb(c: CallbackQuery) -> None:
            _, action, gid = c.data.split(":")
            g = self.state.get(gid)
            lang = g.lang if g else "ru"
            if g is None:
                await c.answer(t(lang, "group_no_gathering"), show_alert=True)
                return
            if action == "in":
                g.members.setdefault(c.from_user.id, Member(name=c.from_user.first_name or "Участник"))
                await self.refresh_gathering(c.bot, g)
                await c.answer()
            elif action == "pick":
                if not g.wishes() and not g.members:
                    await c.answer(t(lang, "group_need_more"), show_alert=True)
                    return
                await c.answer(t(lang, "thinking"))
                await self.pick(c.bot, g)
            elif action == "fin":
                await c.answer()
                await self.finish(c.bot, g)

        @r.callback_query(F.data.startswith("tw:"))
        async def tour_card(c: CallbackQuery) -> None:
            await c.answer()
            await self.send_tour(c.message, c.data[3:], self.state.session(c.from_user.id).lang)

        @r.callback_query(F.data.startswith("tl:") | F.data.startswith("tm:"))
        async def tour_start(c: CallbackQuery) -> None:
            await c.answer()
            await self.start_walk(c, c.data[3:], live=c.data.startswith("tl:"))

        @r.callback_query(F.data.startswith("ta:"))
        async def tour_here(c: CallbackQuery) -> None:
            _, tour_id, stop_id = c.data.split(":", 2)
            s = self.state.session(c.from_user.id)
            await c.answer()
            if s.walk is None or s.walk.tour_id != tour_id:
                await c.message.answer(t(s.lang, "walk_none_active"))
                return
            tour = await self.get_tour(tour_id, s.lang)
            if tour:
                await self.walk_arrive(c.bot, c.message.chat.id, s, tour, stop_id)

        @r.callback_query(F.data.startswith("ts:"))
        async def tour_story(c: CallbackQuery) -> None:
            _, tour_id, stop_id = c.data.split(":", 2)
            lang = self.state.session(c.from_user.id).lang
            await c.answer(t(lang, "thinking"))
            story = await self.call(self.backend.tour_story, tour_id, stop_id, lang)
            if story:
                await c.message.answer(t(lang, "walk_story", text=escape(story["text"])), link_preview_options=NO_PREVIEW)

        @r.callback_query(F.data.startswith("te:"))
        async def tour_end(c: CallbackQuery) -> None:
            s = self.state.session(c.from_user.id)
            await c.answer()
            if s.walk is None:
                await c.message.answer(t(s.lang, "walk_none_active"))
                return
            await self.walk_finish(c.bot, c.message.chat.id, s)

        @r.poll_answer()
        async def poll_answer(p: PollAnswer) -> None:
            gid = self.state.by_poll.get(p.poll_id)
            g = self.state.get(gid) if gid else None
            if g is None or p.user is None:
                return
            if p.option_ids:
                g.votes[p.user.id] = p.option_ids[0]
            else:
                g.votes.pop(p.user.id, None)

        # ------------------------------------------------ inline mode
        @r.inline_query()
        async def inline(q: InlineQuery) -> None:
            lang = user_lang(q.from_user.language_code)
            text = q.query.strip()
            hint = InlineQueryResultArticle(id="hint", title=t(lang, "inline_hint"),
                                            input_message_content=InputTextMessageContent(message_text=t(lang, "hello")))
            if len(text) < 3:
                await q.answer([hint], cache_time=60, is_personal=True)
                return
            try:
                res = await self.call(self.backend.ask, text=text, lang=lang, format="structured", llm=False)
            except Exception:
                await q.answer([hint], cache_time=10, is_personal=True)
                return
            ans = res["answer"]
            if not ans.get("anchor"):
                await q.answer([hint], cache_time=10, is_personal=True)
                return
            out = [InlineQueryResultArticle(
                id=f"{ans['id']}-{i}", title=f"{i + 1}. {it['title']}", description=f"{walk_text(it)} · {it['status']}"[:200],
                input_message_content=InputTextMessageContent(message_text=item_html(ans, i), parse_mode=ParseMode.HTML,
                                                              link_preview_options=NO_PREVIEW),
                reply_markup=markup(route_keyboard(it["lat"], it["lon"], lang)))
                for i, it in enumerate(ans["items"])]
            await q.answer(out or [InlineQueryResultArticle(
                id="none", title=t(lang, "inline_none"),
                input_message_content=InputTextMessageContent(message_text=t(lang, "inline_none")))],
                cache_time=30, is_personal=True)

    # ------------------------------------------------------------- flows
    async def surprise(self, m: Message) -> None:
        s = self.state.session(m.from_user.id)
        loc = s.location()
        ans = await self.send_answer(m, "удиви меня", s.lang, lat=loc[0] if loc else None,
                                     lon=loc[1] if loc else None, surprise=True)
        if ans is not None and ans.get("anchor") is None:
            s.pending = "удиви меня"
            await m.answer(t(s.lang, "need_place"), reply_markup=self.reply_kb(s.lang))

    async def send_walks(self, m: Message, lang: str, loc) -> None:
        walks = await self.call(self.backend.walks, loc[0], loc[1], lang)
        if not walks:
            await m.answer(t(lang, "walks_none"))
            return
        lines = [t(lang, "walks_title")]
        for w in walks:
            stops = " → ".join(s["title"] for s in (w["answer"].get("route") or {}).get("stops", [])) or \
                " · ".join(i["title"] for i in w["answer"]["items"])
            lines.append(f"\n<b>{escape(w['title'])}</b> · {escape(w['meta'])}\n{escape(stops)}")
        await m.answer("\n".join(lines), link_preview_options=NO_PREVIEW)

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

    async def get_tour(self, tour_id: str, lang: str) -> dict | None:
        key = (tour_id, lang)
        if key not in self._tours:
            tour = await self.call(self.backend.tour, tour_id, lang)
            if tour is None:
                return None
            self._tours[key] = tour
        return self._tours[key]

    def tour_url(self, tour_id: str) -> tuple[str, str] | None:
        if self.webapp_url:
            return "web_app", f"{self.webapp_url}?tour={tour_id}"
        if self.bot_username:
            return "url", f"https://t.me/{self.bot_username}?startapp=t_{tour_id}"
        return None

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

    async def send_tour(self, m: Message, tour_id: str, lang: str) -> None:
        tour = await self.get_tour(tour_id, lang)
        if tour is None:
            await m.answer(t(lang, "tours_none"))
            return
        rows = [[InlineKeyboardButton(text=t(lang, "btn_live"), callback_data=f"tl:{tour_id}"),
                 InlineKeyboardButton(text=t(lang, "btn_manual"), callback_data=f"tm:{tour_id}")]]
        link = self.tour_url(tour_id)
        if link and link[0] == "web_app" and m.chat.type == ChatType.PRIVATE:
            rows.append([InlineKeyboardButton(text=t(lang, "btn_tour_map"), web_app=WebAppInfo(url=link[1]))])
        elif link:
            rows.append([InlineKeyboardButton(text=t(lang, "btn_tour_map"), url=link[1] if link[0] == "url"
                                              else f"https://t.me/{self.bot_username}?startapp=t_{tour_id}")])
        await m.answer(tour_html(tour), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), link_preview_options=NO_PREVIEW)

    def walk_kb(self, tour: dict, s, lang: str, stop: dict | None, nxt: dict | None) -> InlineKeyboardMarkup:
        rows = []
        if nxt is not None:
            name = nxt["name"] if len(nxt["name"]) <= 40 else nxt["name"][:38] + "…"
            rows.append([InlineKeyboardButton(text=t(lang, "btn_here", name=name), callback_data=f"ta:{tour['id']}:{nxt['id']}")])
        last = []
        if stop is not None and (settings.ANTHROPIC_API_KEY or settings.API_URL):
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

    async def start_walk(self, c: CallbackQuery, tour_id: str, live: bool) -> None:
        s = self.state.session(c.from_user.id)
        tour = await self.get_tour(tour_id, s.lang)
        if tour is None:
            await c.message.answer(t(s.lang, "tours_none"))
            return
        s.walk = Walk(tour_id=tour_id, title=tour["title"], total=len(tour["stops_list"]), live=live)
        first = tour["stops_list"][0]
        await self.sticker(c.bot, c.message.chat.id, "happy")
        text = t(s.lang, "tour_start_live" if live else "tour_start_manual", first=escape(first["name"]))
        if live:
            text += "\n\n" + t(s.lang, "tour_privacy")
        await c.message.answer(text, reply_markup=self.walk_kb(tour, s, s.lang, None, first), link_preview_options=NO_PREVIEW)

    async def walk_location(self, m: Message, force: bool = False) -> None:
        """One location (a sent point or a live-location tick): did we reach a stop? Used in memory only."""
        s = self.state.session(m.from_user.id)
        w = s.walk
        now = time.time()
        if w is None or (not force and now - w.last_update < self.walk_interval_s):
            return
        w.last_update = now
        lat, lon = m.location.latitude, m.location.longitude
        tour = await self.get_tour(w.tour_id, s.lang)
        if tour is None:
            return
        pr = await self.call(self.backend.tour_progress, w.tour_id, lat, lon, list(w.visited),
                             m.location.horizontal_accuracy)
        if pr["arrived"]:
            await self.walk_arrive(m.bot, m.chat.id, s, tour, pr["arrived"], next_m=pr["next_m"], next_min=pr["next_min"])
            return
        if pr["off_route"] and pr["next"] and now - w.last_off_route > 300:
            w.last_off_route = now
            nxt = next(x for x in tour["stops_list"] if x["id"] == pr["next"])
            await m.answer(t(s.lang, "walk_off_route", name=escape(nxt["name"]), dist=meters(pr["next_m"], s.lang)))
        if now - w.last_nudge_check > 180:
            w.last_nudge_check = now
            nxt = next((x for x in tour["stops_list"] if x["id"] == pr["next"]), None)
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
        await self.sticker(bot, chat_id, "celebrate")
        await bot.send_message(chat_id, t(s.lang, "walk_done", title=escape(w.title), stops=len(w.visited), total=w.total,
                                          facts=w.facts))

    async def start_gathering(self, m: Message, text: str) -> None:
        lang = user_lang(m.from_user.language_code)
        g = self.state.new_gathering(m.chat.id, lang, text)
        g.members[m.from_user.id] = Member(name=m.from_user.first_name or "Участник")
        where = f" — {escape(text)}" if text else ""
        msg = await m.answer(t(lang, "group_start", where=where), reply_markup=self.group_kb(g.gid, len(g.members), lang))
        self.state.bind_message(g, msg.message_id)

    async def refresh_gathering(self, bot: Bot, g) -> None:
        where = f" — {escape(g.text)}" if g.text else ""
        lines = [t(g.lang, "group_start", where=where)]
        wishes = [escape(m.name) + (f" — {escape(m.wish)}" if m.wish else "") + (" · геолокация" if m.lat is not None else "")
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

    async def pick(self, bot: Bot, g) -> None:
        members = g.wishes() or [f"{m.name}: просто погулять" for m in g.members.values()]
        body = {"text": g.text or "куда пойти вместе", "members": members, "lang": g.lang, "format": "structured"}
        pts = g.points()
        if pts:
            body["lat"] = sum(p[0] for p in pts) / len(pts)
            body["lon"] = sum(p[1] for p in pts) / len(pts)
        res = await self.call(self.backend.ask, **body)
        ans = res["answer"]
        if not ans.get("anchor"):
            await bot.send_message(g.chat_id, t(g.lang, "group_need_place"))
            return
        g.answer = ans
        text = answer_html(ans, g.lang)
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
                                       options=[it["title"][:100] for it in ans["items"]], is_anonymous=False,
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
                                           text=t(g.lang, "group_final"), callback_data=f"g:fin:{g.gid}")]]))
            g.poll_id = poll.poll.id
            self.state.by_poll[g.poll_id] = g.gid
        elif ans["items"]:
            await self.finish(bot, g)

    async def finish(self, bot: Bot, g) -> None:
        ans = g.answer
        if not ans or not ans["items"]:
            await bot.send_message(g.chat_id, t(g.lang, "group_no_gathering"))
            return
        tally = [0] * len(ans["items"])
        for opt in g.votes.values():
            if 0 <= opt < len(tally):
                tally[opt] += 1
        win = max(range(len(tally)), key=lambda i: (tally[i], -i))
        it = ans["items"][win]
        leave = hm(it["leave_by"]) if it.get("leave_by") else hm(ans["at"])
        text = t(g.lang, "group_winner", title=escape(it["title"]), leave=t(g.lang, "group_leave", time=leave, walk=walk_text(it)))
        await bot.send_message(g.chat_id, text, reply_markup=markup(route_keyboard(it["lat"], it["lon"], g.lang)))


async def setup_bot(bot: Bot, qb: QydyrBot) -> None:
    me = await bot.get_me()
    qb.bot_username = me.username or ""
    await bot.set_my_commands([BotCommand(command="start", description="Начать"),
                               BotCommand(command="map", description="Открыть карту"),
                               BotCommand(command="surprise", description="Удиви меня"),
                               BotCommand(command="walks", description="Прогулки с гидом и рядом"),
                               BotCommand(command="lang", description="Язык / Тіл / Language")],
                              scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands([BotCommand(command="go", description="Подобрать место для компании")],
                              scope=BotCommandScopeAllGroupChats())
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
