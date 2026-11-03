"""Turn each FAIL/ERROR block of a unittest run into a GitHub Actions error annotation.

Annotations show on the run's summary page without logging in, unlike the job logs.
Usage (CI only): python tests/gh_annotate.py test-output.txt
"""

import re
import sys
from pathlib import Path

LIMIT = 10   # GitHub shows at most 10 error annotations per step


def escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    output = Path(sys.argv[1]).read_text(encoding="utf-8", errors="replace")
    blocks = re.findall(r"^={20,}\n((?:FAIL|ERROR): .+?)(?=^={20,}|^-{20,}\nRan )", output, re.S | re.M)
    for block in blocks[:LIMIT]:
        title, _, body = block.partition("\n")
        print(f"::error title={escape(title.strip())}::{escape(body.strip('-').strip()[-3000:])}")
    if not blocks:
        print("::error title=unittest failed::" + escape(output[-3000:]))


if __name__ == "__main__":
    main()
