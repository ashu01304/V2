import sqlite3
import pandas as pd
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "market_data.db"

class DatabaseManager:
    def __init__(self):
        self.conn = sqlite3.connect(DB_PATH)
        self.cursor = self.conn.cursor()
        self._history_cache = {}
        self._initialize_tables()

    def _initialize_tables(self):
        self.cursor.execute("""
            CREATE TABLE IF NOT EXISTS seac_settlements (
                symbol          TEXT NOT NULL,
                contract_code   TEXT NOT NULL,
                trading_date    TEXT NOT NULL,
                price           REAL,
                PRIMARY KEY (symbol, contract_code, trading_date)
            )
        """)
        self.conn.commit()

    def save_seac_batch(self, rows):
        query = "INSERT OR REPLACE INTO seac_settlements VALUES (?, ?, ?, ?)"
        self.cursor.executemany(query, rows)
        self.conn.commit()
        self._history_cache.clear()

    def get_contract_history(self, symbol, codes):
        data = {}
        for code in codes:
            key = (symbol, code)
            if key in self._history_cache:
                df = self._history_cache[key]
                if not df.empty:
                    data[code] = df
                continue
            query = """
                SELECT trading_date as Date, price as Close 
                FROM seac_settlements 
                WHERE symbol = ? AND contract_code = ?
                ORDER BY trading_date ASC
            """
            df = pd.read_sql_query(query, self.conn, params=(symbol, code))
            if not df.empty:
                df['Date'] = pd.to_datetime(df['Date'])
                df.set_index('Date', inplace=True)
            self._history_cache[key] = df
            if not df.empty:
                data[code] = df
        return data

    def close(self):
        self.conn.close()
