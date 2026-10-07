"""Release hygiene: the CHANGELOG must document the version being shipped.
(For ~30 releases an unchecked str.replace wrote nothing and nobody noticed.)"""
from pathlib import Path

from rssgate import __version__

ROOT = Path(__file__).resolve().parent.parent


def test_changelog_has_entry_for_current_version():
    text = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{__version__}]" in text, (
        f"CHANGELOG.md has no '## [{__version__}]' entry - write it before "
        "tagging")


def test_changelog_newest_entry_is_current_version():
    import re
    heads = re.findall(r"^## \[(\d+\.\d+\.\d+)\]", (ROOT / "CHANGELOG.md")
                       .read_text(), re.M)
    assert heads and heads[0] == __version__, heads[:3]
