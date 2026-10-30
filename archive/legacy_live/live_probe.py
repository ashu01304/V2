"""Isolated, manually controlled Lightstreamer diagnostics; no recording or synthesis."""

from collections import deque
from datetime import datetime, timezone
import threading


def catalogue_rows(payload):
    rows = payload.get("data") if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise ValueError("Expected a JSON list of instruments, or an object with a data list; inspect the raw response below.")
    return rows


def catalogue_item(row):
    instrument = row.get("instid")
    return "TT-" + str(instrument) if instrument not in (None, "") else ""


class StreamProbe:
    def __init__(self):
        self.lock = threading.RLock()
        self.client = None
        self.generation = 0
        self.rows = {}
        self.events = deque(maxlen=200)
        self.status = "Stopped"
        self.connection_status = "Stopped"
        self.settings = None

    def event(self, generation, message, status=None):
        with self.lock:
            if generation != self.generation:
                return
            if status:
                self.status = status
            self.events.append({"time": datetime.now(timezone.utc).isoformat(), "message": message})

    def stop(self):
        with self.lock:
            self.generation += 1
            client, self.client = self.client, None
            self.status = "Stopped"
            self.connection_status = "Stopped"
        if client is not None:
            client.disconnect()

    def start(self, settings, password=""):
        from lightstreamer.client import LightstreamerClient, Subscription, ClientListener, SubscriptionListener

        self.stop()
        with self.lock:
            generation = self.generation
            self.rows.clear()
            self.events.clear()
            self.settings = dict(settings)
            self.status = "Connecting"
            self.connection_status = "Connecting"
        probe = self

        class ConnectionListener(ClientListener):
            def onStatusChange(self, status):
                with probe.lock:
                    if generation != probe.generation:
                        return
                    probe.connection_status = status
                probe.event(generation, "Connection: " + status, status)

            def onServerError(self, code, message):
                probe.event(generation, f"Server error {code}: {message}", "Server error")

        class PriceListener(SubscriptionListener):
            def onSubscription(self):
                probe.event(generation, "Subscription accepted")

            def onSubscriptionError(self, code, message):
                probe.event(generation, f"Subscription error {code}: {message}", "Subscription error")

            def onEndOfSnapshot(self, item_name, item_pos):
                probe.event(generation, f"Snapshot complete: {item_name or item_pos}")

            def onItemLostUpdates(self, item_name, item_pos, lost):
                probe.event(generation, f"Lost {lost} updates: {item_name or item_pos}")

            def onItemUpdate(self, update):
                try:
                    item = update.getItemName() or str(update.getItemPos())
                    values = {field: update.getValue(field) for field in settings["fields"]}
                    snapshot = update.isSnapshot()
                    received = datetime.now(timezone.utc).isoformat()
                    with probe.lock:
                        if generation != probe.generation:
                            return
                        previous = probe.rows.get(item, {})
                        probe.rows[item] = {
                            "item": item, "received_at_utc": received,
                            "snapshot": snapshot, "updates": previous.get("updates", 0) + 1,
                            "snapshot_updates": previous.get("snapshot_updates", 0) + int(snapshot),
                            "live_updates": previous.get("live_updates", 0) + int(not snapshot),
                            "last_live_received_at_utc": previous.get("last_live_received_at_utc") if snapshot else received,
                            **values,
                        }
                except Exception as error:
                    probe.event(generation, f"Update decoding error: {error}")

        client = LightstreamerClient(settings["server"], settings["adapter_set"] or None)
        try:
            if settings["user"]:
                client.connectionDetails.setUser(settings["user"])
            if password:
                client.connectionDetails.setPassword(password)
            client.addListener(ConnectionListener())
            batch_size = settings.get("batch_size") or len(settings["items"])
            if batch_size < 1:
                raise ValueError("Subscription batch size must be positive")
            subscriptions = []
            for start in range(0, len(settings["items"]), batch_size):
                subscription = Subscription(settings["mode"], settings["items"][start:start + batch_size], settings["fields"])
                if settings["data_adapter"]:
                    subscription.setDataAdapter(settings["data_adapter"])
                subscription.setRequestedSnapshot(settings["snapshot"])
                if settings["frequency"]:
                    subscription.setRequestedMaxFrequency(settings["frequency"])
                subscription.addListener(PriceListener())
                subscriptions.append(subscription)
            with self.lock:
                self.client = client
            self.event(generation, f"Subscribing to {len(settings['items'])} items in {len(subscriptions)} batches")
            for subscription in subscriptions:
                client.subscribe(subscription)
            client.connect()
        except Exception:
            self.stop()
            raise

    def snapshot(self):
        with self.lock:
            return {"status": self.status, "connection_status": self.connection_status, "settings": self.settings,
                    "rows": [dict(row) for row in self.rows.values()], "events": list(self.events)}
