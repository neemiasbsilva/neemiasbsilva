"""Rebuild README.md from arXiv and the GitHub API."""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

USER = "neemiasbsilva"
README = Path(__file__).parent / "README.md"

ARXIV_API = "https://export.arxiv.org/api/query"
ARXIV_AUTHOR = 'au:"da Silva, Neemias"'
GITHUB_API = f"https://api.github.com/users/{USER}/repos"
ATOM = "{http://www.w3.org/2005/Atom}"

SKIP_REPOS = {USER}

PAPER_LINKS = {
    "2604.28048": "https://minds-lab-utfpr.github.io/MLLMs-persona-evaluation/",
}

OLDER_PAPERS = [
    (
        "A convolutional neural network approach for counting and geolocating "
        "citrus-trees in UAV multispectral imagery",
        "https://doi.org/10.1016/j.isprsjprs.2019.12.010",
        "2020-02-18",
    ),
    (
        "Regression in Convolutional Neural Networks applied to Plant Leaf Counting",
        "https://doi.org/10.5753/wvc.2019.7627",
        "2019-09-09",
    ),
]


def fetch(url, headers=None, retries=3, backoff=5):
    """GET url and return the raw response body.

    Retries on timeouts, 429s and 5xx responses, since the arXiv and GitHub
    APIs occasionally stall, rate-limit or bounce a request under load.
    Retrying here absorbs that transient flakiness instead of letting it
    fail the whole build. Other 4xx responses mean the request itself is
    wrong, so those raise immediately instead of retrying.
    """
    request = urllib.request.Request(url, headers=headers or {})
    request.add_header("User-Agent", f"{USER}-profile-readme")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except (TimeoutError, urllib.error.URLError) as error:
            is_bad_request = (
                isinstance(error, urllib.error.HTTPError)
                and error.code < 500
                and error.code != 429
            )
            if is_bad_request or attempt == retries - 1:
                raise
            wait = backoff * (attempt + 1)
            if isinstance(error, urllib.error.HTTPError) and error.code == 429:
                wait = max(wait, int(error.headers.get("Retry-After", 0)))
            time.sleep(wait)


def fetch_papers():
    """Return arXiv preprints merged with OLDER_PAPERS, newest first.

    An entry is kept only when one of its author names contains "Neemias",
    since arXiv also indexes an unrelated Neemias Martins. Links point at
    the versionless abstract page so they survive a new revision, and dates
    come from first submission so a revision cannot reorder the list.
    PAPER_LINKS redirects individual papers to a project page instead.

    OLDER_PAPERS covers the two pre-2021 papers the arXiv API does not
    return. They are published and final, so they are held here rather than
    fetched from a second, noisier metadata source.
    """
    query = urllib.parse.urlencode(
        {
            "search_query": ARXIV_AUTHOR,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": 40,
        }
    )
    feed = ET.fromstring(fetch(f"{ARXIV_API}?{query}"))

    papers = []
    for entry in feed.findall(f"{ATOM}entry"):
        authors = [a.findtext(f"{ATOM}name", "") for a in entry.findall(f"{ATOM}author")]
        if not any("Neemias" in name for name in authors):
            continue
        url = re.sub(r"v\d+$", "", entry.findtext(f"{ATOM}id", ""))
        url = url.replace("http://arxiv.org/", "https://arxiv.org/", 1)
        url = PAPER_LINKS.get(url.rsplit("/", 1)[-1], url)
        title = " ".join(entry.findtext(f"{ATOM}title", "").split())
        date = entry.findtext(f"{ATOM}published", "")[:10]
        papers.append((title, url, date))

    papers.extend(OLDER_PAPERS)
    papers.sort(key=lambda paper: paper[2], reverse=True)
    return papers


def fetch_projects(limit=5):
    """Return own repos, most recently pushed first.

    Forks, archived repos and repos with no description are dropped.
    SKIP_REPOS excludes this repo, which the workflow commits to on every
    run and would otherwise pin itself to the top of its own list.
    """
    query = urllib.parse.urlencode({"per_page": 100, "sort": "pushed", "type": "owner"})
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    repos = json.loads(fetch(f"{GITHUB_API}?{query}", headers))

    projects = []
    for repo in repos:
        if repo["fork"] or repo["archived"] or repo["name"] in SKIP_REPOS:
            continue
        if not (repo.get("description") or "").strip():
            continue
        projects.append(
            (repo["name"], repo["html_url"], repo["pushed_at"][:10], repo["description"].strip())
        )

    projects.sort(key=lambda project: project[2], reverse=True)
    return projects[:limit]


def shorten(text, length=110):
    """Collapse whitespace and truncate on a word boundary."""
    text = " ".join(text.split())
    if len(text) <= length:
        return text
    return text[:length].rsplit(" ", 1)[0] + "…"


def render_papers(papers):
    """Render papers as blank-line separated Markdown paragraphs."""
    return "\n\n".join(f"[{title}]({url}) - {date}" for title, url, date in papers)


def render_projects(projects):
    """Render projects as blank-line separated Markdown paragraphs."""
    return "\n\n".join(
        f"[{name}]({url}) - {date}  \n{shorten(description)}"
        for name, url, date, description in projects
    )


def replace_chunk(content, marker, chunk):
    """Replace the text between the marker's start and end comments."""
    pattern = re.compile(rf"<!-- {marker} starts -->.*<!-- {marker} ends -->", re.DOTALL)
    return pattern.sub(f"<!-- {marker} starts -->\n{chunk}\n<!-- {marker} ends -->", content)


def main():
    """Rewrite each section, keeping the previous text when a source fails.

    A section whose source is unreachable keeps its existing content and the
    process exits non-zero, so an outage turns the build badge red instead of
    blanking a live section of the profile.
    """
    content = README.read_text()
    failed = False

    for marker, fetcher, renderer in (
        ("papers", fetch_papers, render_papers),
        ("projects", fetch_projects, render_projects),
    ):
        try:
            content = replace_chunk(content, marker, renderer(fetcher()))
        except (urllib.error.URLError, ET.ParseError, ValueError, KeyError, OSError) as error:
            print(f"warning: keeping existing {marker} section: {error}", file=sys.stderr)
            failed = True

    README.write_text(content)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
