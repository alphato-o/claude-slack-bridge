"""
slack_files.py — download Slack message attachments so Claude can see them.

Slack ``url_private`` is auth-gated; a bot token (``files:read`` scope) is required to
fetch. Both execution modes use this:
  • session mode (claude -p): download into a container-local dir, inject the paths into
    the prompt so claude -p can Read the images with its own Read tool.
  • brain mode: download into the bind-mounted BRAIN_DIR so the native brain Reads them.
"""

import logging
import os
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


def download(files: list, dest_dir: Path, bot_token: str) -> list[dict]:
    """Download each Slack file attachment into *dest_dir*.

    Returns [{name, path, mimetype, is_image, [error]}]. Images are what Claude can view;
    other file types (PDF, etc.) are fetched too — the model decides what to do with them.
    """
    out: list[dict] = []
    if not files:
        return out
    dest_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        url = f.get("url_private_download") or f.get("url_private")
        name = f.get("name") or f.get("id", "file")
        mimetype = f.get("mimetype", "")
        is_image = mimetype.startswith("image/")
        if not url:
            continue
        local = dest_dir / f"{f.get('id', 'f')}_{name}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {bot_token}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                local.write_bytes(r.read())
            out.append({"name": name, "path": str(local), "mimetype": mimetype, "is_image": is_image})
            logger.info("downloaded Slack attachment %s (%s) → %s", name, mimetype, local)
        except Exception as exc:
            logger.warning("failed to download Slack attachment %s: %s", name, exc)
            out.append({"name": name, "path": None, "mimetype": mimetype, "is_image": is_image,
                        "error": str(exc)})
    return out


def prompt_note(downloaded: list[dict]) -> str:
    """Build a prompt addendum telling claude -p about attached files it can Read.
    Empty string when there are no successfully-downloaded files."""
    ok = [d for d in downloaded if d.get("path")]
    if not ok:
        return ""
    lines = ["\n\n## Attachments in this Slack message (use the Read tool to view images):"]
    for d in ok:
        kind = "image" if d["is_image"] else d.get("mimetype", "file")
        lines.append(f"- {kind}: {d['path']}")
    return "\n".join(lines)
