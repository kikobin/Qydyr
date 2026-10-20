"""The bot against a stricter fake Telegram: it rejects what the real one rejects (bare '<' in HTML, callback_data
over 64 bytes, equal poll options, edits that change nothing) and yields on every call, so concurrent taps race
as they do under polling. Regression tests for the group flow, languages, locations, walks and odd inputs."""
import asyncio
import re
from datetime import datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (AnswerCallbackQuery, AnswerInlineQuery, EditMessageReplyMarkup, EditMessageText,
                             SendMessage, SendPoll, SendSticker, SetMyCommands, StopPoll)
from aiogram.types import (CallbackQuery, Chat, Document, InlineKeyboardMarkup, InlineQuery, Location, Message,
                           PhotoSize, Poll, PollAnswer, PollOption, Update, User)

from qydyr_data import settings
from qydyr_data.bot.app import QydyrBot, setup_bot
from qydyr_data.bot.backend import LocalBackend
from qydyr_data.bot.fmt import answer_html, poll_options
from qydyr_data.bot.texts import T

from .conftest import TEST_DSN
from .test_bot import FRIENDS, GROUP, NOW, PRIVATE, USER, FakeSession, ids, msg
from .test_pipeline_db import load_everything
from .test_tours import sample_tours  # noqa: F401  (fixture)

BOT_USER = User(id=42, is_bot=True, first_name="Qydyr", username="qydyr_bot")
TAG = re.compile(r"</?(b|i|u|s|a|em|strong|code|pre|blockquote|tg-spoiler|span|ins|del|strike)\b")


class StrictSession(FakeSession):
    def __init__(self):
        super().__init__()
        self.shown: dict[tuple, str] = {}       # (chat, message) -> what is on screen: text and keyboard

    @staticmethod
    def _kb(mk) -> str:
        if isinstance(mk, InlineKeyboardMarkup):
            for b in (b for row in mk.inline_keyboard for b in row):
                assert b.callback_data is None or len(b.callback_data.encode()) <= 64, b.callback_data
            return mk.model_dump_json()
        return ""

    async def make_request(self, bot, method, timeout=None):
        await asyncio.sleep(0)                  # a real request yields: lets concurrent handlers interleave
        text = getattr(method, "text", None)
        if isinstance(method, (SendMessage, EditMessageText)):
            bad = [m.start() for m in re.finditer("<", text) if not TAG.match(text, m.start())]
            if bad:
                raise TelegramBadRequest(method, f"can't parse entities at {bad[0]}: {text[bad[0]:bad[0] + 20]!r}")
        if isinstance(method, SendPoll):
            opts = [o if isinstance(o, str) else o.text for o in method.options]
            if len(set(opts)) < len(opts):
                raise TelegramBadRequest(method, "POLL_OPTION_DUPLICATE")
            self.calls.append(method)
            poll = Poll(id=f"poll{next(ids)}", question=method.question, total_voter_count=0, is_closed=False,
                        options=[PollOption(text=o, voter_count=0, persistent_id=str(k)) for k, o in enumerate(opts)],
                        is_anonymous=False, type="regular", allows_multiple_answers=False, allows_revoting=True,
                        members_only=False)
            return Message(message_id=next(ids), date=datetime.now(), chat=Chat(id=method.chat_id, type="group"), poll=poll)
        if isinstance(method, (EditMessageText, EditMessageReplyMarkup)):
            key = (method.chat_id, method.message_id)
            now = (text if text is not None else self.shown.get(key, ("", ""))[0], self._kb(method.reply_markup))
            if self.shown.get(key) == now:
                self.calls.append(method)
                raise TelegramBadRequest(method, "Bad Request: message is not modified")
            self.shown[key] = now
        res = await super().make_request(bot, method, timeout)
        if isinstance(method, SendMessage):
            self.shown[(method.chat_id, res.message_id)] = (text, self._kb(method.reply_markup))
        return res

    def answered(self, cq_id: str) -> list[AnswerCallbackQuery]:
        return [c for c in self.sent(AnswerCallbackQuery) if c.callback_query_id == cq_id]


@pytest.fixture
def env(db, sample_places, sample_tours, monkeypatch):  # noqa: F811
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_DSN)
    monkeypatch.setattr(settings, "DEMO_NOW", NOW.isoformat())
    monkeypatch.setattr(settings, "WEATHER_FILE", str(settings.ROOT / "tests/fixtures/weather_mild.json"))
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    session = StrictSession()
    bot = Bot("42:TEST", session=session, default=DefaultBotProperties(parse_mode="HTML"))
    qb = QydyrBot(LocalBackend(), bot_username="qydyr_bot", webapp_url="https://qydyr.example/app/")
    qb.walk_interval_s = 0
    dp = Dispatcher()
    dp.include_router(qb.router)
    return SimpleNamespace(bot=bot, dp=dp, qb=qb, s=session)


def feed(e, **upd):
    return asyncio.run(e.dp.feed_update(e.bot, Update(update_id=next(ids), **upd)))


def tap(e, data, message, user=USER):
    cq = CallbackQuery(id=str(next(ids)), from_user=user, chat_instance="t", message=message, data=data)
    feed(e, callback_query=cq)
    assert e.s.answered(cq.id), f"{data}: the button keeps spinning"
    return cq


def shown(e, call, chat=PRIVATE):
    """The message Telegram returned for a recorded SendMessage."""
    mid = max(m for (c, m), (text, _) in e.s.shown.items() if c == call.chat_id and text == call.text)
    return Message(message_id=mid, date=datetime.now(), chat=chat, from_user=BOT_USER, text=call.text)


def gathering(e):
    g = e.qb.state.current(GROUP.id)
    return g, Message(message_id=g.message_id, date=datetime.now(), chat=GROUP, from_user=BOT_USER, text="…")


def texts(e, since=0):
    return [c.text for c in e.s.calls[since:] if isinstance(c, SendMessage)]


def buttons(call):
    return [b for row in call.reply_markup.inline_keyboard for b in row]


# ------------------------------------------------------------------ group
def test_group_takes_the_place_from_a_reply(env):
    feed(env, message=msg("/go", chat=GROUP))
    g, gm = gathering(env)
    feed(env, message=msg("кофе и тихо", chat=GROUP, reply_to_message=gm))
    feed(env, message=msg("мы у Mega Silk Way", chat=GROUP, user=FRIENDS[0], reply_to_message=gm))
    tap(env, f"g:pick:{g.gid}", gm)
    answer = next(x for x in texts(env) if "Понял так" in x)
    assert answer.startswith("<b>Mega Silk Way</b>") and "Бекнур: кофе" in answer


def test_group_go_text_is_the_starters_wish(env, monkeypatch):
    asks, real = [], env.qb.backend.ask
    monkeypatch.setattr(env.qb.backend, "ask", lambda **b: asks.append(b) or real(**b))
    feed(env, message=msg("/go мы у Mega Silk Way, хотим кофе", chat=GROUP))
    g, gm = gathering(env)
    tap(env, f"g:in:{g.gid}", gm, FRIENDS[0])          # joined without a wish: goes along with /go
    tap(env, f"g:pick:{g.gid}", gm)
    assert asks[-1]["members"] == ["Бекнур: мы у Mega Silk Way, хотим кофе", "Айгерим: мы у Mega Silk Way, хотим кофе"]
    answer = next(x for x in texts(env) if "Понял так" in x)
    assert "кофе" in answer.splitlines()[1] and "просто погулять" not in str(asks)


def test_poll_options_are_unique():
    items = [{"title": "Детская площадка", "walk_min": 7}, {"title": "Mega", "walk_min": 3},
             {"title": "Детская площадка", "walk_min": 8}]
    assert poll_options(items) == ["Детская площадка · 7 мин пешком", "Mega", "Детская площадка · 8 мин пешком"]
    same = poll_options([{"title": "Сквер", "walk_min": 5}] * 2)
    assert len(set(same)) == 2 and same[0] == "1. Сквер · 5 мин пешком"


def test_group_poll_double_tap_votes_and_one_final(env):
    feed(env, message=msg("/go мы у Mega Silk Way, есть час", chat=GROUP))
    g, gm = gathering(env)
    for u, wish in zip(FRIENDS, ["кофе и тихо", "что-то необычное"]):
        feed(env, message=msg(wish, chat=GROUP, user=u, reply_to_message=gm))

    async def double_tap():
        await asyncio.gather(*(env.dp.feed_update(env.bot, Update(update_id=next(ids), callback_query=CallbackQuery(
            id=str(next(ids)), from_user=u, chat_instance="t", message=gm, data=f"g:pick:{g.gid}"))) for u in (USER, FRIENDS[0])))
    asyncio.run(double_tap())
    assert len(env.s.sent(SendPoll)) == 1 and any(c.text == "Уже подбираю…" for c in env.s.sent(AnswerCallbackQuery))
    first = g.poll_id
    for u in FRIENDS:
        feed(env, poll_answer=PollAnswer(poll_id=first, user=u, option_ids=[1], option_persistent_ids=["1"]))
    tap(env, f"g:fin:{g.gid}", gm)
    assert env.s.sent(StopPoll)[-1].message_id == g.poll_message_id
    assert texts(env)[-1].startswith(f"<b>Итог: {g.answer['items'][1]['title']}.</b>")
    n = len(env.s.calls)
    cq = tap(env, f"g:fin:{g.gid}", gm)                    # the result is announced once
    assert env.s.answered(cq.id)[0].text == "Итог уже подведён." and not texts(env, n)
    tap(env, f"g:pick:{g.gid}", gm)                        # a new poll: old votes are gone, old poll is ignored
    assert g.poll_id != first and g.votes == {} and not g.final_done
    feed(env, poll_answer=PollAnswer(poll_id=first, user=USER, option_ids=[2], option_persistent_ids=["2"]))
    assert g.votes == {}


def test_group_pick_failure_is_said_in_the_group(env, monkeypatch):
    feed(env, message=msg("/go мы у Mega Silk Way", chat=GROUP))
    g, gm = gathering(env)
    monkeypatch.setattr(env.qb.backend, "ask", lambda **b: 1 / 0)
    tap(env, f"g:pick:{g.gid}", gm)
    assert "Не получилось подобрать" in texts(env)[-1] and not g.picking


def test_group_language_is_set_in_the_group(env):
    feed(env, message=msg("/lang", chat=GROUP))
    tap(env, "l:kk", shown(env, env.s.sent(SendMessage)[-1], GROUP))
    feed(env, message=msg("/go", chat=GROUP))
    assert texts(env)[-1].startswith("<b>Компания жинап жатырмын")
    assert env.qb.state.session(USER.id).lang == "ru"      # the person's own language did not change


# ------------------------------------------------------------------ languages and locations
def test_chosen_language_survives_start_and_newcomers_get_theirs(env):
    feed(env, message=msg("/lang"))
    tap(env, "l:en", shown(env, env.s.sent(SendMessage)[-1]))
    feed(env, message=msg("/start"))
    assert texts(env)[-1].startswith("<b>Hi! I'm Qydyr")
    john = User(id=11, is_bot=False, first_name="John", language_code="en")
    feed(env, message=msg("coffee please", chat=Chat(id=11, type="private"), user=john))   # no /start first
    assert texts(env)[-1].startswith("Where are you?")


def test_gps_header_start_reset_and_outside_astana(env):
    s = env.qb.state.session(USER.id)
    s.set_location(51.0900, 71.4165)
    s.loc_at -= 10 * 60
    feed(env, message=msg("хочу кофе"))
    assert texts(env)[-1].startswith("<b>ваша геолокация (10 мин назад)</b> · ")
    feed(env, message=msg(location=Location(latitude=51.0900, longitude=71.4165)))
    assert texts(env)[-1].startswith("<b>ваша геолокация</b> · ")
    feed(env, message=msg("/start"))
    assert s.location() is not None      # the last point stands until a new one, /start or not
    feed(env, message=msg(location=Location(latitude=43.238, longitude=76.945)))   # Almaty
    assert "знаю только Астану" in texts(env)[-1] and s.location() is None


def test_answer_labels_follow_the_language(env):
    john = User(id=11, is_bot=False, first_name="John", language_code="en")
    env.qb.state.session(11, "en").set_location(51.0900, 71.4165)
    feed(env, message=msg("coffee", chat=Chat(id=11, type="private"), user=john))
    text = texts(env)[-1]
    assert "Why:" in text and "Time:" in text and "Source:" in text and "How I chose" in text
    assert not any(ru in text for ru in ("Понял так", "Почему:", "Сколько:", "Мини-маршрут", "мин пешком"))
    assert set(T["kk"]) == set(T["ru"]) == set(T["en"])
    for k in T["ru"]:
        assert len({frozenset(re.findall(r"{(\w+)}", T[lang][k])) for lang in T}) == 1, k


# ------------------------------------------------------------------ answers, chips, errors
def test_chips_switch_off_and_double_back_is_quiet(env):
    env.qb.state.session(USER.id).set_location(51.0900, 71.4165)
    feed(env, message=msg("хочу кофе"))
    ans = env.s.sent(SendMessage)[-1]
    m = shown(env, ans)
    kids = next(b for b in buttons(ans) if b.text == "+ с ребёнком")
    tap(env, kids.callback_data, m)
    assert any(b.text == "✕ с ребёнком" for b in buttons(env.s.sent(EditMessageText)[-1]))
    budget = next(b for b in buttons(ans) if "изменить" in b.text)
    tap(env, budget.callback_data, m)
    back = next(b for b in buttons(env.s.sent(EditMessageReplyMarkup)[-1]) if b.callback_data.startswith("k:"))
    tap(env, back.callback_data, m)
    tap(env, back.callback_data, m)                         # "message is not modified": answered, no error


def test_nothing_found_is_said_once():
    note = "Рядом не нашлось подходящего с учётом времени, часов работы и погоды."
    ans = {"id": "x", "at": "2026-10-23T19:10:00+05:00", "anchor": {"label": "Хан Шатыр", "source": "place"},
           "chips": [], "notes": [note], "items": [], "funnel": [{"key": "nearby", "label": "мест рядом", "n": 284}]}
    html = answer_html(ans)
    assert html.count("Рядом не нашлось") == 1 and "284 места рядом" in html


def test_a_failing_handler_answers_the_button(env, monkeypatch):
    def boom(aid):
        raise RuntimeError("store down")
    monkeypatch.setattr(env.qb.backend, "answer", boom)
    cq = tap(env, "w:abc", Message(message_id=1, date=datetime.now(), chat=PRIVATE, from_user=BOT_USER, text="x"))
    a = env.s.answered(cq.id)[0]
    assert a.show_alert and a.text.startswith("Не получилось")


# ------------------------------------------------------------------ walks
def test_walks_after_a_typed_place_can_be_walked(env):
    feed(env, message=msg("/walks"))
    assert texts(env)[-1].startswith("Где вы?")
    feed(env, message=msg("я у Mega Silk Way"))
    walks = env.s.sent(SendMessage)[-1]
    assert "Прогулки рядом" in walks.text and env.qb.state.session(USER.id).pending is None
    guided = [b for b in buttons(walks) if (b.callback_data or "").startswith("tw:walk_")]
    assert guided and any((b.url or "").startswith("https://www.google.com/maps/dir/") for b in buttons(walks))
    tap(env, guided[0].callback_data, shown(env, walks))
    card = env.s.sent(SendMessage)[-1]
    tap(env, buttons(card)[1].callback_data, shown(env, card))          # without location
    first = env.s.sent(SendMessage)[-1]
    assert first.text.startswith("Первая точка") and buttons(first)[0].callback_data.startswith("ta:walk_")
    n = len(env.s.calls)
    feed(env, message=msg(location=Location(latitude=51.1283, longitude=71.4305)))   # far: where to go
    assert any(x.startswith("Следующая точка:") for x in texts(env, n))
    n = len(env.s.calls)
    tap(env, buttons(first)[-1].callback_data, shown(env, first))       # finish at 0 stops: no party
    assert "остановлена" in texts(env, n)[-1] and not any(isinstance(c, SendSticker) for c in env.s.calls[n:])


def test_a_walk_in_progress_is_not_dropped_silently(env):
    feed(env, message=msg("/start t_test_expo"))
    card = env.s.sent(SendMessage)[-1]
    tap(env, "tm:test_expo", shown(env, card))
    tap(env, "tl:test_expo", shown(env, card))
    ask = env.s.sent(SendMessage)[-1]
    assert "Сейчас идёт прогулка" in ask.text and [b.callback_data for b in buttons(ask)] == ["tl:test_expo:f", "tc"]
    tap(env, "tl:test_expo:f", shown(env, ask))
    assert env.qb.state.session(USER.id).walk.live


# ------------------------------------------------------------------ odd inputs
def test_odd_inputs_get_a_hint(env, monkeypatch):
    from qydyr_data.pulse import PULSE
    feed(env, message=msg("/foo"))
    assert texts(env)[-1].startswith("Такой команды нет")
    feed(env, message=msg(document=Document(file_id="d", file_unique_id="du")))
    assert texts(env)[-1].startswith("Я понимаю текст и геолокацию")
    feed(env, message=msg("/walks", chat=GROUP))
    assert "в личном чате" in texts(env)[-1]
    feed(env, message=msg("@Qydyr_Bot кофе", chat=GROUP))
    assert env.qb.state.current(GROUP.id).text == "кофе"
    feed(env, inline_query=InlineQuery(id="q", from_user=USER, query="ко", offset=""))
    hint = env.s.sent(AnswerInlineQuery)[-1]
    assert "Сәлем" not in hint.results[0].input_message_content.message_text and hint.button.start_parameter == "inline"
    env.qb.state.session(USER.id).set_location(51.0900, 71.4165)
    feed(env, message=msg("хочу кофе"))
    ans = env.s.sent(SendMessage)[-1]
    # the feedback buttons are gone from new answers; a tap on an older answer's button still counts once
    aid = ans.reply_markup.inline_keyboard[0][0].callback_data.split(":")[1]
    assert not any((b.callback_data or "").startswith("f:") for b in buttons(ans))
    before = PULSE.votes["up"]
    tap(env, f"f:{aid}:up", shown(env, ans))
    tap(env, f"f:{aid}:up", shown(env, ans))
    assert PULSE.votes["up"] == before + 1
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", "test")
    monkeypatch.setattr(env.qb.backend, "whereami", lambda img, lang: {
        "found": True, "place": "Bar <Rock & Roll>", "landmark": {"confidence": "medium"}, "answer": env.qb.backend.answer(
            ans.reply_markup.inline_keyboard[0][0].callback_data.split(":")[1])})

    async def download(*a, **kw):
        return None
    monkeypatch.setattr(Bot, "download", download)
    feed(env, message=msg(photo=[PhotoSize(file_id="p", file_unique_id="pu", width=10, height=10)]))
    assert any("Bar &lt;Rock &amp; Roll&gt;" in x for x in texts(env))


def test_without_https_there_is_no_map_button(env):
    qb = QydyrBot(LocalBackend(), bot_username="qydyr_bot")
    dp = Dispatcher()
    dp.include_router(qb.router)
    e = SimpleNamespace(bot=env.bot, dp=dp, qb=qb, s=env.s)
    qb.state.session(USER.id).set_location(51.0900, 71.4165)
    feed(e, message=msg("хочу кофе"))
    assert not any(b.url and "startapp" in b.url for b in buttons(env.s.sent(SendMessage)[-1]))
    feed(e, message=msg("/map"))
    assert texts(e)[-1].startswith("Карта пока недоступна")


def test_commands_are_set_in_three_languages(env, monkeypatch):
    from qydyr_data import settings
    monkeypatch.setattr(settings, "BOT_USERNAME", "")
    asyncio.run(setup_bot(env.bot, env.qb))
    assert settings.BOT_USERNAME == env.qb.bot_username        # the Mini App learns it without QYDYR_BOT_USERNAME
    sets = env.s.sent(SetMyCommands)
    assert {c.language_code for c in sets} == {None, "ru", "kk", "en"}
    kk = next(c for c in sets if c.language_code == "kk" and any(x.command == "walks" for x in c.commands))
    assert next(x for x in kk.commands if x.command == "walks").description == T["kk"]["cmd_walks"]
