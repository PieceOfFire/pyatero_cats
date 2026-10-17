"""Bounded display caches and context for ordered, retry-safe menu transitions."""

import time
from collections import OrderedDict
from contextvars import ContextVar

from aiogram.types import Update

current_update_id: ContextVar[int] = ContextVar("current_update_id", default=0)

NAVIGATION = frozenset(
    {
        "back_main",
        "profile",
        "rewards",
        "change_nick",
        "leaderboard",
        "top",
        "nick_manual",
        "promo_enter",
        "reward_sub",
    }
)


def is_navigation(update: Update) -> bool:
    query = update.callback_query
    if query is None or query.message is None or query.message.chat.type != "private":
        return False
    data = query.data or ""
    if data in NAVIGATION:
        return True
    prefix, _, offset = data.partition(":")
    return prefix == "collection" and offset.isascii() and offset.isdigit() and len(offset) <= 8


class TTLCache:
    def __init__(self, ttl: float, capacity: int = 2048, clock=time.monotonic):
        self.ttl, self.capacity, self.clock = ttl, capacity, clock
        self.entries = OrderedDict()

    def get(self, key):
        item = self.entries.get(key)
        if item is None:
            return None
        expires, value = item
        if expires <= self.clock():
            self.entries.pop(key, None)
            return None
        self.entries.move_to_end(key)
        return value

    def put(self, key, value):
        self.entries[key] = (self.clock() + self.ttl, value)
        self.entries.move_to_end(key)
        while len(self.entries) > self.capacity:
            self.entries.popitem(last=False)

    def discard(self, key):
        self.entries.pop(key, None)

    def clear(self):
        self.entries.clear()
