from dataclasses import dataclass

from telegram.ext import ContextTypes

from .config import Config
from .docs import DocFetcher
from .llm import Answerer
from .sheets import Sheet
from .store import Store


@dataclass
class Services:
    cfg: Config
    sheet: Sheet
    docs: DocFetcher
    store: Store
    llm: Answerer

    @property
    def group_id(self) -> int | None:
        """The team inbox group: ADMIN_GROUP_ID from the environment, else the group linked with /setup."""
        if self.cfg.admin_group_id:
            return self.cfg.admin_group_id
        stored = self.store.get_value("admin_group_id")
        return int(stored) if stored else None


def services(context: ContextTypes.DEFAULT_TYPE) -> Services:
    return context.application.bot_data["svc"]
