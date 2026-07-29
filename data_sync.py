import requests
import json
import re
import urllib3
from database.manager import DatabaseManager

# Disable SSL warnings for corporate site
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

def sync():
    url = "https://insight.corp.hertshtengroup.com/structured/compressed/allseacdata.js"
    print(f"Connecting to {url}...")
    
    try:
        r = requests.get(url, verify=False, timeout=60)
        r.raise_for_status()
        text = r.text
        print(f"Downloaded {len(text) / 1024 / 1024:.2f} MB of data.")

        start_index = text.find('{')
        end_index = text.rfind('}') + 1
        
        if start_index == -1 or end_index == 0:
            print("Error: Could not find JSON boundaries in the file.")
            return

        json_str = text[start_index:end_index]
        data = json.loads(json_str)
        print("JSON parsed successfully.")

        rows = []
        for sym, dates in data.items():
            for dt, contracts in dates.items():
                for code, price in contracts.items():
                    rows.append((sym, code, dt, price))
        
        if not rows:
            print("Warning: No data rows were generated from the JSON.")
            return

        print(f"Prepared {len(rows)} records for the database.")

        db = DatabaseManager()
        db.cursor.execute("DELETE FROM seac_settlements")
        db.save_seac_batch(rows)
        
        db.cursor.execute("SELECT count(*) FROM seac_settlements")
        count = db.cursor.fetchone()[0]
        db.close()
        
        print(f"Sync complete! Total records now in Database: {count}")

    except Exception as e:
        print(f"An error occurred during sync: {e}")

if __name__ == "__main__":
    sync()