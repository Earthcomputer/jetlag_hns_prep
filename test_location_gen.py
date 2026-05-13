import json
import random

# Tweak these two values as needed.
NUM_PLACES = 200
OUTPUT_FILE = "places.json"

MIN_LAT = 53.291667
MAX_LAT = 53.816667
MIN_LON = -2.3
MAX_LON = -1.33333

ADJECTIVES = [
    "Golden", "Blue", "Red", "Green", "Silver", "Bright", "Hidden", "Royal",
    "Urban", "Grand", "Sunny", "Cedar", "Stone", "North", "West", "Maple",
]

NOUNS = [
    "Garden", "Bistro", "Kitchen", "Cafe", "Deli", "Market", "House", "Tavern",
    "Table", "Harbor", "Corner", "Lounge", "Terrace", "Pavilion", "Spice", "Nook",
]

SUFFIXES = [
    "Hall", "Place", "Bar", "Eatery", "Grill", "Bakery", "Room", "Studio",
]

def random_name(existing: set[str]) -> str:
    while True:
        name = f"{random.choice(ADJECTIVES)} {random.choice(NOUNS)} {random.choice(SUFFIXES)}"
        if name not in existing:
            existing.add(name)
            return name

def random_place(existing_names: set[str]) -> dict:
    return {
        "location": {
            "latitude": round(random.uniform(MIN_LAT, MAX_LAT), 7),
            "longitude": round(random.uniform(MIN_LON, MAX_LON), 7),
        },
        "displayName": {
            "text": random_name(existing_names),
            "languageCode": "en",
        },
    }

def main() -> None:
    existing_names = set()
    data = {
        "places": [random_place(existing_names) for _ in range(NUM_PLACES)]
    }

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

if __name__ == "__main__":
    main()
