"""
Wiki probe — diagnostic script for the wiki-help feature.

Run once before you rely on the wiki-help chat feature. Confirms that:

  1. ``AZURE_DEVOPS_*`` env vars are set (ORG, PROJECT, PAT)
  2. The PAT has "Wiki (Read)" scope and can reach the target project
  3. The wiki id is resolvable (either via env or auto-detected)
  4. The table of contents fetches successfully

If any of these fail, the script prints a focused error so you know
exactly what to fix. If everything works, it prints the TOC preview so
you can eyeball the pages the chat_agent will see.

Usage
-----
    (venv) $ python scripts/probe_wiki.py

Exit codes:
    0  = all good, chat wiki-help will work
    1  = configuration missing / disabled
    2  = ADO reachable but wiki fetch failed
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

# Allow running from repo root: `python scripts/probe_wiki.py`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
from services.wiki_service import get_wiki_service  # noqa: E402


BANNER = "─" * 60


def _line(label: str, value: str) -> str:
    return f"  {label:<28} {value}"


async def _run() -> int:
    print(BANNER)
    print("  Wiki-help feature — configuration probe")
    print(BANNER)
    print(_line("AZURE_DEVOPS_ORG",       config.AZURE_DEVOPS_ORG or "(missing)"))
    print(_line("AZURE_DEVOPS_PROJECT",   config.AZURE_DEVOPS_PROJECT or "(missing)"))
    print(_line("AZURE_DEVOPS_PAT",       "set" if config.AZURE_DEVOPS_PAT else "(missing)"))
    print(_line("AZURE_DEVOPS_WIKI_ID",   config.AZURE_DEVOPS_WIKI_ID or "(auto-detect)"))
    print(_line("WIKI_CACHE_TTL_SECONDS", str(config.WIKI_CACHE_TTL_SECONDS)))
    print(_line("WIKI_ASSISTANT_NAME",    config.WIKI_ASSISTANT_NAME or "(auto)"))
    print()

    svc = get_wiki_service()

    if not svc.is_enabled:
        print("  ✗ Wiki service is DISABLED.")
        if (config.AZURE_DEVOPS_WIKI_ID or "").upper() == "DISABLED":
            print("    Reason: AZURE_DEVOPS_WIKI_ID=DISABLED in .env")
        else:
            print("    Reason: one of ORG / PROJECT / PAT is empty")
        print("  → Chat will run exactly as before (zero overhead).")
        return 1

    print(f"  Assistant persona: {svc.assistant_name}")
    print()
    print("  → Fetching table of contents from Azure DevOps…")
    toc = await svc.get_toc()

    if not toc:
        print("  ✗ TOC fetch returned empty.")
        print("    Likely causes:")
        print("      • PAT missing 'Wiki (Read)' scope")
        print("      • Account lacks read permission on the project wiki")
        print("      • Wrong AZURE_DEVOPS_ORG / PROJECT")
        print("    Check the loguru warning above for the exact HTTP error.")
        return 2

    lines = toc.splitlines()
    print(f"  ✓ TOC OK — {len(lines)} pages")
    print(BANNER)
    for row in lines[:40]:
        print(f"    {row}")
    if len(lines) > 40:
        print(f"    … and {len(lines) - 40} more")
    print(BANNER)
    print("  ✓ Wiki-help feature is ready.")
    print("    Try in chat:  \"how do I set up my workstation?\"")
    return 0


if __name__ == "__main__":
    try:
        rc = asyncio.run(_run())
    except KeyboardInterrupt:
        rc = 130
    sys.exit(rc)
