"""The Telegram bot end to end without Telegram: a fake aiogram session records every API call, the
assistant runs for real on the PostGIS fixtures."""
import asyncio
import itertools
from datetime import datetime

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (AnswerInlineQuery, EditMessageReplyMarkup, EditMessageText, GetMe,
                             SendMessage, SendPoll, SendVenue)
from aiogram.types import (CallbackQuery, Chat, InlineQuery, Location, Message, Poll, PollAnswer, PollOption, Update,
                           User)

from qydyr_data import settings
from qydyr_data.bot.app import QydyrBot
from qydyr_data.bot.backend import LocalBackend
from qydyr_data.bot.fmt import answer_html, answer_keyboard, chip_keyboard, route_keyboard

from .conftest import TEST_DSN
from .test_pipeline_db import load_everything

NOW = datetime(2026, 10, 23, 19, 10)
ids = itertools.count(100)


class FakeSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        chat = Chat(id=getattr(method, "chat_id", 1) or 1, type="private")
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Qydyr", username="qydyr_bot")
        if isinstance(method, (SendMessage, SendVenue)):
            return Message(message_id=next(ids), date=datetime.now(), chat=chat, text=getattr(method, "text", ""))
        if isinstance(method, SendPoll):
            poll = Poll(id="poll1", question=method.question, options=[PollOption(text=str(o), voter_count=0, persistent_id=str(k)) for k, o in enumerate(method.options)],
                        total_voter_count=0, is_closed=False, is_anonymous=False, type="regular", allows_multiple_answers=False,
                        allows_revoting=True, members_only=False)
            return Message(message_id=next(ids), date=datetime.now(), chat=chat, poll=poll)
        if isinstance(method, EditMessageText):
            return Message(message_id=method.message_id or 1, date=datetime.now(), chat=chat, text=method.text)
        return True

    async def close(self):
        pass

    async def stream_content(self, *a, **kw):  # pragma: no cover
        yield b""

    def sent(self, kind):
        return [c for c in self.calls if isinstance(c, kind)]


@pytest.fixture
def harness(db, sample_places, monkeypatch):
    load_everything(db, sample_places)
    monkeypatch.setattr(settings, "DATABASE_URL", TEST_DSN)
    monkeypatch.setattr(settings, "DEMO_NOW", NOW.isoformat())
    monkeypatch.setattr(settings, "WEATHER_FILE", str(settings.ROOT / "tests/fixtures/weather_mild.json"))
    monkeypatch.setattr(settings, "ANTHROPIC_API_KEY", None)
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    qb = QydyrBot(LocalBackend(), bot_username="qydyr_bot", webapp_url="https://qydyr.example/app/")
    dp = Dispatcher()
    dp.include_router(qb.router)
    return bot, dp, qb, session


USER = User(id=7, is_bot=False, first_name="Бекнур", language_code="ru")
FRIENDS = [User(id=8, is_bot=False, first_name="Айгерим"), User(id=9, is_bot=False, first_name="Данияр")]
PRIVATE = Chat(id=7, type="private")
GROUP = Chat(id=-100, type="supergroup", title="Пятница")


def msg(text=None, chat=PRIVATE, user=USER, **kw):
    return Message(message_id=next(ids), date=datetime.now(), chat=chat, from_user=user, text=text, **kw)


def as_message(call, chat):
    """A recorded SendMessage call as the Message Telegram would have returned."""
    return Message(message_id=next(ids), date=datetime.now(), chat=chat, text=call.text,
                   from_user=User(id=42, is_bot=True, first_name="Qydyr"))


def feed(bot, dp, **upd):
    return asyncio.run(dp.feed_update(bot, Update(update_id=next(ids), **upd)))


def test_private_flow_location_answer_chips_route(harness):
    bot, dp, qb, s = harness
    feed(bot, dp, message=msg("/start"))
    hello = s.sent(SendMessage)[0]
    assert "Сәлем" in hello.text and hello.reply_markup.keyboard[0][0].request_location
    # a request without a location: the bot asks where you are and remembers the request
    feed(bot, dp, message=msg("хочу кофе и тихо"))
    assert "Где вы" in s.sent(SendMessage)[-1].text and qb.state.session(7).pending == "хочу кофе и тихо"
    # the location arrives: the pending request is answered from there
    feed(bot, dp, message=msg(location=Location(latitude=51.0900, longitude=71.4165)))
    ans_msg = s.sent(SendMessage)[-1]
    assert "Кофейня «Тихий угол»" in ans_msg.text and "<blockquote expandable>" in ans_msg.text
    assert "📍" not in ans_msg.text and "Геолокация округлена" in ans_msg.text
    kb = ans_msg.reply_markup.inline_keyboard
    assert kb[1][0].web_app.url.startswith("https://qydyr.example/app/?answer=")
    aid = kb[0][0].callback_data.split(":")[1]
    # tap "45–75 мин · изменить", then "2 часа"
    chip_btn = next(b for row in kb for b in row if b.callback_data and b.callback_data.startswith(f"c:{aid}:"))
    cq = CallbackQuery(id="1", from_user=USER, chat_instance="x", message=as_message(ans_msg, PRIVATE),
                       data=chip_btn.callback_data)
    feed(bot, dp, callback_query=cq)
    menu = s.sent(EditMessageReplyMarkup)[-1].reply_markup.inline_keyboard
    two_hours = next(b for row in menu for b in row if "2 часа" in b.text)
    feed(bot, dp, callback_query=cq.model_copy(update={"data": two_hours.callback_data, "id": "2"}))
    edited = s.sent(EditMessageText)[-1]
    assert "Понял так: до 2 ч" in edited.text
    # route to the first place: a venue with 2GIS / Yandex / Google buttons
    feed(bot, dp, callback_query=cq.model_copy(update={"data": f"m:{aid}:0", "id": "3"}))
    venue = s.sent(SendVenue)[-1]
    urls = [r[0].url for r in venue.reply_markup.inline_keyboard]
    assert any("2gis.kz/astana/directions" in u for u in urls) and any("google.com/maps/dir" in u for u in urls)
    # why not
    feed(bot, dp, callback_query=cq.model_copy(update={"data": f"w:{aid}", "id": "4"}))
    assert "Почему не другое" in s.sent(SendMessage)[-1].text


def test_surprise_and_walks(harness):
    bot, dp, qb, s = harness
    qb.state.session(7).set_location(51.0900, 71.4165)
    feed(bot, dp, message=msg("Удиви меня"))
    assert s.sent(SendMessage)[-1].text.count("<b>1.") == 1
    feed(bot, dp, message=msg("/walks"))
    assert "Прогулки рядом" in s.sent(SendMessage)[-1].text


def test_group_gathering_poll_and_result(harness):
    bot, dp, qb, s = harness
    feed(bot, dp, message=msg("/go мы у Mega Silk Way, есть час", chat=GROUP))
    gathering = s.sent(SendMessage)[-1]
    assert "Собираю компанию" in gathering.text and "Я иду · 1" in gathering.reply_markup.inline_keyboard[0][0].text
    g = qb.state.current(GROUP.id)
    bot_msg = Message(message_id=g.message_id, date=datetime.now(), chat=GROUP, from_user=User(id=42, is_bot=True, first_name="Qydyr"))
    feed(bot, dp, message=msg("кофе и тихо", chat=GROUP, user=FRIENDS[0], reply_to_message=bot_msg))
    feed(bot, dp, message=msg("что-то необычное", chat=GROUP, user=FRIENDS[1], reply_to_message=bot_msg))
    feed(bot, dp, message=msg("мне Wi-Fi", chat=GROUP, user=USER, reply_to_message=bot_msg))
    assert "Айгерим — кофе и тихо" in s.sent(EditMessageText)[-1].text
    assert len(g.wishes()) == 3
    cq = CallbackQuery(id="9", from_user=USER, chat_instance="y", message=as_message(gathering, GROUP),
                       data=f"g:pick:{g.gid}")
    feed(bot, dp, callback_query=cq)
    result = next(m for m in reversed(s.sent(SendMessage)) if "Каждому" in m.text)
    assert "Айгерим: кофе — " in result.text
    assert all(b.url is None or b.url.startswith("https://t.me/qydyr_bot") for row in result.reply_markup.inline_keyboard for b in row)
    poll = s.sent(SendPoll)[-1]
    assert poll.question == "Куда идём?" and poll.is_anonymous is False and len(poll.options) >= 2
    feed(bot, dp, poll_answer=PollAnswer(poll_id="poll1", user=FRIENDS[0], option_ids=[1], option_persistent_ids=["1"]))
    feed(bot, dp, poll_answer=PollAnswer(poll_id="poll1", user=FRIENDS[1], option_ids=[1], option_persistent_ids=["1"]))
    feed(bot, dp, callback_query=cq.model_copy(update={"data": f"g:fin:{g.gid}", "id": "10"}))
    final = s.sent(SendMessage)[-1]
    second = g.answer["items"][1]["title"]
    assert final.text.startswith(f"<b>Итог: {second}.</b>") and "Выходите в" in final.text


def test_added_to_group_starts_gathering(harness):
    bot, dp, qb, s = harness
    feed(bot, dp, message=msg("/start go", chat=GROUP))
    assert "Собираю компанию" in s.sent(SendMessage)[-1].text and qb.state.current(GROUP.id) is not None


def test_group_html_in_wishes_and_title_is_escaped(harness):
    bot, dp, qb, s = harness
    feed(bot, dp, message=msg("/go кофе <b & тихо", chat=GROUP))
    assert "кофе &lt;b &amp; тихо" in s.sent(SendMessage)[-1].text
    g = qb.state.current(GROUP.id)
    bot_msg = Message(message_id=g.message_id, date=datetime.now(), chat=GROUP, from_user=User(id=42, is_bot=True, first_name="Qydyr"))
    feed(bot, dp, message=msg("<3 пицца & кино", chat=GROUP, user=FRIENDS[0], reply_to_message=bot_msg))
    assert "&lt;3 пицца &amp; кино" in s.sent(EditMessageText)[-1].text


def test_group_pick_without_place_asks_in_group_terms(harness):
    bot, dp, qb, s = harness
    feed(bot, dp, message=msg("/go", chat=GROUP))
    gathering = s.sent(SendMessage)[-1]
    g = qb.state.current(GROUP.id)
    cq = CallbackQuery(id="11", from_user=USER, chat_instance="y", message=as_message(gathering, GROUP), data=f"g:pick:{g.gid}")
    feed(bot, dp, callback_query=cq)
    last = s.sent(SendMessage)[-1].text
    assert "Моя геопозиция" in last and "ответьте на это сообщение местом" in last
    geo = s.sent(SendMessage)[-1].reply_markup.inline_keyboard[0][0]
    assert geo.url == f"https://t.me/{qb.bot_username}?start=gl_{g.gid}"   # groups have no location button: the private chat has
    # the reply to that "Где вы?" is where the group is: it picks at once, no second tap
    pid = next(mid for (chat, mid), gid in qb.state.by_message.items() if gid == g.gid and mid != g.message_id)
    prompt = Message(message_id=pid, date=datetime.now(), chat=GROUP, from_user=User(id=42, is_bot=True, first_name="Qydyr"))
    n = len(s.sent(SendMessage))
    feed(bot, dp, message=msg("мы у Mega Silk Way", chat=GROUP, reply_to_message=prompt))
    assert any("Mega" in m.text or "<b>1." in m.text for m in s.sent(SendMessage)[n:]), [m.text[:80] for m in s.sent(SendMessage)[n:]]


def test_bot_added_to_group_says_hello(harness):
    from aiogram.types import ChatMemberLeft, ChatMemberMember, ChatMemberUpdated
    bot, dp, qb, s = harness
    me = User(id=42, is_bot=True, first_name="Qydyr")
    feed(bot, dp, my_chat_member=ChatMemberUpdated(
        chat=GROUP, from_user=USER, date=datetime.now(),
        old_chat_member=ChatMemberLeft(user=me), new_chat_member=ChatMemberMember(user=me)))
    assert "/go" in s.sent(SendMessage)[-1].text


def test_inline_mode(harness):
    bot, dp, qb, s = harness
    feed(bot, dp, inline_query=InlineQuery(id="q1", from_user=USER, query="кофе у Mega Silk Way", offset=""))
    res = s.sent(AnswerInlineQuery)[-1]
    assert res.results and res.results[0].title.startswith("1.") and res.results[0].reply_markup
    feed(bot, dp, inline_query=InlineQuery(id="q2", from_user=USER, query="кофе", offset=""))
    assert s.sent(AnswerInlineQuery)[-1].results[0].id == "hint"
    # the last location sent to the bot answers "кофе" typed inline
    qb.state.session(USER.id).set_location(51.0900, 71.4165)
    feed(bot, dp, inline_query=InlineQuery(id="q3", from_user=USER, query="кофе", offset=""))
    assert s.sent(AnswerInlineQuery)[-1].results[0].id != "hint"


def test_fmt_without_telegram():
    ans = {"id": "abc", "at": "2026-10-23T19:10:00+05:00", "anchor": {"label": "Mega Silk Way", "source": "place"},
           "chips": [{"key": "budget", "label": "45–75 мин", "kind": "time", "active": True, "assumed": True,
                      "options": [{"label": "1 час", "patch": {"budget_max": 60}, "selected": False}]},
                     {"key": "kids", "label": "с ребёнком", "kind": "who", "active": False, "assumed": False,
                      "options": [{"label": "с ребёнком", "patch": {"with_kids": True}, "selected": False}]}],
           "notes": [], "items": [], "funnel": [], "rejected": [], "context": {}, "group_notes": []}
    html = answer_html(ans)
    assert "Mega Silk Way" in html and "Понял так: 45–75 мин?" in html and "Рядом ничего не подошло" in html
    kb = answer_keyboard(ans)
    assert any(b["callback_data"] == "c:abc:0" for row in kb for b in row if "callback_data" in b)
    assert chip_keyboard(ans, 1) == []          # a switch is applied at once
    assert len(route_keyboard(51.1, 71.4)) == 3
