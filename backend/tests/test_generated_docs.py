import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import gen_docs


def test_generated_docs_are_up_to_date() -> None:
    assert gen_docs.main(["--check"]) == 0, "run: python tools/gen_docs.py"
