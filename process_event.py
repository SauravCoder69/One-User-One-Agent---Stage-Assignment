
import json
from pathlib import Path

from app.database import initialize_database, process_event_batch

EVENTS_PATH = Path("data/events.jsonl")
BATCH_SIZE = 1000


def process_events():
    initialize_database()

    total = 0
    new_events = 0

    batch = []

    with EVENTS_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            event = json.loads(line)
            batch.append(event)
            total += 1

            if len(batch) >= BATCH_SIZE:
                new_events += process_event_batch(batch)
                batch.clear()

                if total % 10000 == 0:
                    print(f"Processed: {total} events")

        if batch:
            new_events += process_event_batch(batch)

    print("\nProcessing complete!")
    print("Total events:", total)
    print("New events saved:", new_events)
    print("Duplicates skipped:", total - new_events)


if __name__ == "__main__":
    process_events()
