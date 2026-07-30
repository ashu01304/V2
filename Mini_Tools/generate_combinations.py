import json
from pathlib import Path


HORIZON = 14
OUTPUT = Path(__file__).parents[1] / "combinations.json"

PATTERNS = {
    "Defly": [
        [1, -3, 3, -1],
        [1, 0, -3, 3, 0, -1],
        [1, 0, -3, 0, 3, 0, -1],
    ],
    "Butterfly": [
        [1, -2, 1],
        [1, 0, -2, 0, 1],
    ],
    "Condor": [
        [1, -1, -1, 1],
        [1, 0, -1, -1, 0, 1],
    ],
    "Spread": [
        [1, -1],
        [1, 0, -1],
    ],
    "1-1-2+2+1-1": [
        [1, -1, -2, 2, 1, -1],
        [1, 0, -1, 0, -2, 0, 2, 0, 1, 0, -1],
    ],
}


combinations = {
    name: [
        [0] * start + pattern + [0] * (HORIZON - len(pattern) - start)
        for pattern in patterns
        for start in range(HORIZON - len(pattern) + 1)
    ]
    for name, patterns in PATTERNS.items()
}

lines = ["{"]
for family_number, (name, family) in enumerate(combinations.items()):
    lines.append(f"  {json.dumps(name)}: [")
    for combination_number, combination in enumerate(family):
        comma = "," if combination_number < len(family) - 1 else ""
        lines.append(f"    {json.dumps(combination)}{comma}")
    comma = "," if family_number < len(combinations) - 1 else ""
    lines.append(f"  ]{comma}")
lines.append("}")

OUTPUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
print(f"Generated {sum(map(len, combinations.values()))} combinations in {OUTPUT}")
