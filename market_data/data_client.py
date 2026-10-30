"""Common dashboard entry point. Separate resolvers own source-specific logic."""
from contextlib import contextmanager
from .config import DATABASE_PATH,LIVE_MANAGER_URL
from .queries import MarketData
from . import seac_resolver,minute_resolver,live_resolver


class DataClient:
    def __init__(self,database_path=DATABASE_PATH,*,live_url=LIVE_MANAGER_URL,owner=None,database=None):
        self.database_path=database_path
        self.database=database
        self.live_resolver=live_resolver.LiveResolver(live_url,owner)

    @contextmanager
    def _database(self):
        if self.database is not None:
            yield self.database
            return
        # Open a fresh reader snapshot per historical request; no stale long-lived DB.
        database=MarketData(self.database_path)
        try:
            yield database
        finally:
            database.close()

    def plan(self,structure,source):
        """Inspect a resolver's recipe without opening a database or making HTTP calls."""
        resolvers={'seac':seac_resolver,'minute':minute_resolver,'live':live_resolver}
        if source not in resolvers:
            raise ValueError('Choose seac, minute or live')
        return resolvers[source].plan(structure)

    def seac(self,structure,*,start,end):
        self.plan(structure,'seac')
        with self._database() as database:
            return seac_resolver.resolve(structure,database,start=start,end=end)

    def minute(self,structure,*,start,end,interval='15min'):
        self.plan(structure,'minute')
        with self._database() as database:
            return minute_resolver.resolve(structure,database,start=start,end=end,interval=interval)

    def live(self,structure,*,include_alternatives=False):
        return self.live_resolver.resolve(structure,include_alternatives=include_alternatives)

    def live_many(self,structures,*,include_recipes=False,include_alternatives=False):
        return self.live_resolver.resolve_many(structures,include_recipes=include_recipes,include_alternatives=include_alternatives)

    def minute_snapshots(self,selections,timestamps):
        """Selections are (key, complete_structure); exact timestamps, never filled."""
        with self._database() as database:
            return minute_resolver.snapshots(selections,timestamps,database)

    def minute_histograms(self,filters,*,start,end):
        with self._database() as database:
            return minute_resolver.calendar_histograms(filters,database,start=start,end=end)

    def disconnect(self,structures=None):
        self.live_resolver.disconnect(structures)

    def close(self):
        self.live_resolver.close()

    def __enter__(self):
        return self

    def __exit__(self,*_):
        self.close()
