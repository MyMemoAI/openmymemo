"""Print the failed and errored tests in a pytest JUnit report as GitHub annotations, so they show on the check
run without opening its log:

    uv run pytest --junitxml=report.xml || python scripts/annotate_failures.py report.xml

Exits 1 when the report has failures or can't be read, else 0. A pytest run can fail without either (interrupted,
or no tests collected), so the CI step exits with pytest's own status, not this one's.
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
    names = [f"{c.get('classname', '')}::{c.get('name', '')}" for c in root.iter("testcase")
             if c.find("failure") is not None or c.find("error") is not None]
    if names:  # GitHub shows only the first ten error annotations of a step: this one lists them all
        short = [n if len(n) <= 160 else n[:157] + "..." for n in names]  # one long parametrize id hid the rest
        print(f"::error title={len(names)} failed::{esc(chr(10).join(short))}")
    shown = 1
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            failed += 1
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            detail = (node.get("message") or "") + "\n" + (node.text or "")[-1500:]
            level = "error" if shown < 10 else "warning"  # each level shows ten per step
            shown += 1
            print(f"::{level} title={esc(name)}::{esc(detail.strip())}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
