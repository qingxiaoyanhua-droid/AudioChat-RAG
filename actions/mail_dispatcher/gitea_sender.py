from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

import requests


@dataclass(frozen=True)
class GiteaIssueResult:
    number: int
    url: str


def _env(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def create_issue(
    title: str,
    body: str,
    labels: Optional[list[str]] = None,
) -> GiteaIssueResult:
    """
    Create a Gitea issue in a target repo.

    Required env:
      - GITEA_BASE_URL  e.g. https://git.dtx.openpie.com
      - GITEA_TOKEN     personal access token
      - GITEA_OWNER     org/user
      - GITEA_REPO      repo name
    """
    base = _env("GITEA_BASE_URL").rstrip("/")
    token = _env("GITEA_TOKEN")
    owner = _env("GITEA_OWNER")
    repo = _env("GITEA_REPO")

    url = f"{base}/api[表情]1/repos/{owner}/{repo}/issues"
    headers = {"Authorization": f"token {token}"}
    payload: dict = {"title": title, "body": body}

    # labels (optional): Gitea API expects label IDs in some versions,
    # but some deployments accept label names. If your server requires IDs,
    # we can add a helper to resolve name->id.
    if labels:
        payload["labels"] = labels

    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()

    return GiteaIssueResult(number=int(data["number"]), url=str(data.get("html_url") or data.get("url")))