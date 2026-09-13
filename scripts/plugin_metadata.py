#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
DEFAULT_EXCLUDES = {".github", "artwork", "language-audit", "memorandum", "vthemes"}
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None):
        url = path if path.startswith("https://") else API + path
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Accept", "application/vnd.github+json")
        req.add_header("X-GitHub-Api-Version", "2022-11-28")
        req.add_header("User-Agent", "geeklog-plugin-metadata-audit")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req, timeout=30) as response:
                raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            raise RuntimeError("%s %s -> %s %s" % (method, url, exc.code, body))

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload):
        return self.request("POST", path, payload)

    def put(self, path, payload):
        return self.request("PUT", path, payload)


def paginate(gh, path):
    page = 1
    while True:
        sep = "&" if "?" in path else "?"
        rows = gh.get("%s%sper_page=100&page=%d" % (path, sep, page))
        if not rows:
            return
        for row in rows:
            yield row
        if len(rows) < 100:
            return
        page += 1


def normalize(value):
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def decode_content(obj):
    if not isinstance(obj, dict) or obj.get("encoding") != "base64":
        return ""
    return base64.b64decode(obj.get("content", "")).decode("utf-8", "replace")


def fetch_text(gh, org, repo, path, ref):
    quoted = urllib.parse.quote(path, safe="/")
    obj = gh.get("/repos/%s/%s/contents/%s?ref=%s" % (org, repo, quoted, urllib.parse.quote(ref, safe="")))
    return decode_content(obj)


def find_plugin_id(gh, org, repo, branch, paths):
    for candidate in ("functions.inc", "api.inc"):
        if candidate not in paths:
            continue
        text = fetch_text(gh, org, repo, candidate, branch)
        match = re.search(r"function\s+plugin_geticon_([A-Za-z0-9_]+)\s*\(", text, re.I)
        if match:
            return match.group(1).lower(), text
    return repo.lower(), ""


def image_candidates(paths, plugin_id, repo_name):
    preferred_names = {plugin_id.lower(), repo_name.lower(), normalize(plugin_id), normalize(repo_name)}
    ranked = []
    for path in paths:
        low = path.lower()
        if not low.endswith(IMAGE_EXTS):
            continue
        filename = low.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0]
        score = 100
        if low.startswith("admin/images/"):
            score -= 40
        elif "/images/" in low or low.startswith("images/"):
            score -= 25
        if stem in preferred_names or normalize(stem) in preferred_names:
            score -= 40
        ranked.append((score, path))
    ranked.sort(key=lambda item: (item[0], item[1].lower()))
    return [path for _, path in ranked]


def icon_from_runtime_source(source, paths):
    if not source:
        return ""
    matches = re.findall(
        r"""(?:/plugins/[A-Za-z0-9_.-]+/)?(images/[A-Za-z0-9_./ -]+\.(?:png|jpe?g|gif|svg|webp))""",
        source,
        re.I,
    )
    pathset = {p.lower(): p for p in paths}
    for rel in matches:
        rel = rel.replace("\\", "/").lstrip("/")
        for candidate in ("admin/" + rel, rel):
            if candidate.lower() in pathset:
                return pathset[candidate.lower()]
    return ""


def valid_existing_manifest(text):
    try:
        data = json.loads(text)
    except Exception:
        return False, None
    if not isinstance(data, dict) or data.get("schema") != 1:
        return False, data
    if not isinstance(data.get("id"), str) or not data["id"]:
        return False, data
    if not isinstance(data.get("name"), str) or not data["name"]:
        return False, data
    icon = data.get("icon")
    if icon is not None and (not isinstance(icon, str) or not icon or icon.startswith("/") or ".." in icon.split("/")):
        return False, data
    return True, data


def create_manifest(plugin_id, repo_name, icon):
    data = {"schema": 1, "id": plugin_id, "name": repo_name}
    if icon:
        data["icon"] = icon
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def open_pr_for_manifest(gh, org, repo, default_branch, manifest_text):
    branch = "automation/plugin-metadata"
    base = gh.get("/repos/%s/%s/git/ref/heads/%s" % (org, repo, urllib.parse.quote(default_branch, safe="")))
    sha = base["object"]["sha"]
    try:
        gh.post("/repos/%s/%s/git/refs" % (org, repo), {"ref": "refs/heads/" + branch, "sha": sha})
    except RuntimeError as exc:
        if "Reference already exists" not in str(exc):
            raise

    encoded = base64.b64encode(manifest_text.encode("utf-8")).decode("ascii")
    gh.put("/repos/%s/%s/contents/plugin.json" % (org, repo), {
        "message": "Add static plugin metadata manifest",
        "content": encoded,
        "branch": branch,
    })

    pulls = gh.get("/repos/%s/%s/pulls?state=open&head=%s:%s" % (org, repo, org, urllib.parse.quote(branch, safe="")))
    if pulls:
        return pulls[0]["html_url"]

    pr = gh.post("/repos/%s/%s/pulls" % (org, repo), {
        "title": "Add static plugin metadata manifest",
        "head": branch,
        "base": default_branch,
        "body": "Adds `plugin.json` using the Geeklog plugin metadata convention. The manifest references an existing plugin icon and contains no executable code.",
    })
    return pr["html_url"]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--org", default=os.getenv("PLUGIN_METADATA_ORG", "Geeklog-Plugins"))
    parser.add_argument("--mode", choices=("audit", "pr"), default=os.getenv("PLUGIN_METADATA_MODE", "audit"))
    parser.add_argument("--report", default="PLUGIN_METADATA_REPORT.md")
    args = parser.parse_args()

    token = os.getenv("PLUGIN_METADATA_TOKEN") or os.getenv("GITHUB_TOKEN", "")
    gh = GitHub(token)
    rows = []

    for repo in paginate(gh, "/orgs/%s/repos?type=all" % args.org):
        name = repo["name"]
        if name in DEFAULT_EXCLUDES or repo.get("archived") or repo.get("fork"):
            continue

        branch = repo["default_branch"]
        tree = gh.get("/repos/%s/%s/git/trees/%s?recursive=1" % (args.org, name, urllib.parse.quote(branch, safe="")))
        paths = [item["path"] for item in tree.get("tree", []) if item.get("type") == "blob"]

        if "plugin.json" in paths:
            text = fetch_text(gh, args.org, name, "plugin.json", branch)
            ok, data = valid_existing_manifest(text)
            rows.append({
                "repo": name,
                "status": "OK" if ok else "REVIEW",
                "id": data.get("id", "") if isinstance(data, dict) else "",
                "icon": data.get("icon", "") if isinstance(data, dict) else "",
                "action": "existing manifest" if ok else "invalid plugin.json",
            })
            continue

        plugin_id, runtime_source = find_plugin_id(gh, args.org, name, branch, set(paths))
        icon = icon_from_runtime_source(runtime_source, paths)
        if not icon:
            candidates = image_candidates(paths, plugin_id, name)
            icon = candidates[0] if candidates else ""

        if not icon:
            rows.append({"repo": name, "status": "REVIEW", "id": plugin_id, "icon": "", "action": "no reliable icon found"})
            continue

        manifest = create_manifest(plugin_id, name, icon)
        status = "READY"
        action = "proposal"
        if args.mode == "pr":
            if not os.getenv("PLUGIN_METADATA_TOKEN"):
                status = "REVIEW"
                action = "PLUGIN_METADATA_TOKEN missing; PR not created"
            else:
                try:
                    action = open_pr_for_manifest(gh, args.org, name, branch, manifest)
                    status = "PR"
                except Exception as exc:
                    status = "ERROR"
                    action = str(exc)

        rows.append({"repo": name, "status": status, "id": plugin_id, "icon": icon, "action": action})

    rows.sort(key=lambda row: row["repo"].lower())
    lines = [
        "# Geeklog plugin metadata audit",
        "",
        "Mode: `%s`" % args.mode,
        "",
        "| Repository | Status | Plugin id | Icon | Action |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        def cell(value):
            return str(value).replace("|", "\\|").replace("\n", " ")
        lines.append("| %s | %s | `%s` | `%s` | %s |" % (
            cell(row["repo"]), cell(row["status"]), cell(row["id"]), cell(row["icon"]), cell(row["action"])
        ))

    with open(args.report, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")

    counts = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    print(json.dumps(counts, sort_keys=True))
    return 1 if counts.get("ERROR") else 0


if __name__ == "__main__":
    sys.exit(main())
