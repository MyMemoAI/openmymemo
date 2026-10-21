"""Print the failed and errored tests in a pytest JUnit report as GitHub annotations, so they show on the check
run without opening its log:

    uv run pytest --junitxml=report.xml || python scripts/annotate_failures.py report.xml

Exits 1 when the report has failures (or can't be read), so the step still fails.
"""

import sys
import xml.etree.ElementTree as ET


def esc(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def main() -> None:
    try:
        root = ET.parse(sys.argv[1]).getroot()
    except (OSError, ET.ParseError, IndexError) as e:
        print(f"::error::No test report ({type(e).__name__})")
        sys.exit(1)
    failed = 0
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            failed += 1
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (node.get("message") or "") + "\n" + (node.text or "")[-1500:]
            print(f"::error title={esc(name)}::{esc(detail.strip())}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
