import pandas as pd

class MemoryDatabaseManager:
    """A high-speed database proxy that serves data from RAM."""
    def __init__(self, leg_cache):
        self.leg_cache = leg_cache

    def get_contract_history(self, symbol, codes):
        # Simply returns the pre-loaded data from our dictionary
        return {code: self.leg_cache[code] for code in codes if code in self.leg_cache}
    
    def close(self):
        pass # No connection to close