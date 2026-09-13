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

DEFAULT_EXCLUDES = {
    ".github",
    "artwork",
    "language-audit",
    "memorandum",
    "vthemes",
}

IMAGE_EXTS = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
)

PR_BRANCH = "automation/plugin-metadata"


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None):
        url = path if path.startswith("https://") else API + path

        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        request = urllib.request.Request(
            url,
            data=data,
            method=method,
        )

        request.add_header(
            "Accept",
            "application/vnd.github+json",
        )
        request.add_header(
            "X-GitHub-Api-Version",
            "2022-11-28",
        )
        request.add_header(
            "User-Agent",
            "geeklog-plugin-metadata-audit",
        )

        if self.token:
            request.add_header(
                "Authorization",
                "Bearer " + self.token,
            )

        try:
            with urllib.request.urlopen(
                request,
                timeout=30,
            ) as response:
                raw = response.read().decode("utf-8")

                if not raw:
                    return None

                return json.loads(raw)

        except urllib.error.HTTPError as exc:
            body = exc.read().decode(
                "utf-8",
                "replace",
            )

            raise RuntimeError(
                "%s %s -> %s %s"
                % (
                    method,
                    url,
                    exc.code,
                    body,
                )
            )

        except urllib.error.URLError as exc:
            raise RuntimeError(
                "%s %s -> network error: %s"
                % (
                    method,
                    url,
                    exc,
                )
            )

    def get(self, path):
        return self.request(
            "GET",
            path,
        )

    def post(self, path, payload):
        return self.request(
            "POST",
            path,
            payload,
        )

    def put(self, path, payload):
        return self.request(
            "PUT",
            path,
            payload,
        )


def paginate(gh, path):
    page = 1

    while True:
        separator = "&" if "?" in path else "?"

        rows = gh.get(
            "%s%sper_page=100&page=%d"
            % (
                path,
                separator,
                page,
            )
        )

        if not rows:
            return

        for row in rows:
            yield row

        if len(rows) < 100:
            return

        page += 1


def normalize(value):
    return re.sub(
        r"[^a-z0-9]+",
        "",
        value.lower(),
    )


def decode_content(obj):
    if not isinstance(obj, dict):
        return ""

    if obj.get("encoding") != "base64":
        return ""

    content = obj.get(
        "content",
        "",
    )

    try:
        return base64.b64decode(
            content
        ).decode(
            "utf-8",
            "replace",
        )
    except Exception:
        return ""


def fetch_text(
    gh,
    org,
    repo,
    path,
    ref,
):
    quoted_path = urllib.parse.quote(
        path,
        safe="/",
    )

    quoted_ref = urllib.parse.quote(
        ref,
        safe="",
    )

    obj = gh.get(
        "/repos/%s/%s/contents/%s?ref=%s"
        % (
            org,
            repo,
            quoted_path,
            quoted_ref,
        )
    )

    return decode_content(obj)


def fetch_optional_content(
    gh,
    org,
    repo,
    path,
    ref,
):
    quoted_path = urllib.parse.quote(
        path,
        safe="/",
    )

    quoted_ref = urllib.parse.quote(
        ref,
        safe="",
    )

    try:
        return gh.get(
            "/repos/%s/%s/contents/%s?ref=%s"
            % (
                org,
                repo,
                quoted_path,
                quoted_ref,
            )
        )
    except RuntimeError as exc:
        if "404" in str(exc):
            return None

        raise


def extract_function_body(
    source,
    function_name,
):
    pattern = re.compile(
        r"function\s+"
        + re.escape(function_name)
        + r"\s*\([^)]*\)\s*\{",
        re.I,
    )

    match = pattern.search(source)

    if not match:
        return ""

    start = match.end()
    depth = 1
    position = start

    while position < len(source):
        char = source[position]

        if char == "{":
            depth += 1

        elif char == "}":
            depth -= 1

            if depth == 0:
                return source[
                    start:position
                ]

        position += 1

    return ""


def find_plugin_identity(
    gh,
    org,
    repo,
    branch,
    paths,
):
    candidates = (
        "functions.inc",
        "api.inc",
    )

    for candidate in candidates:
        if candidate not in paths:
            continue

        source = fetch_text(
            gh,
            org,
            repo,
            candidate,
            branch,
        )

        matches = re.findall(
            r"function\s+plugin_geticon_"
            r"([A-Za-z0-9_]+)\s*\(",
            source,
            re.I,
        )

        if matches:
            plugin_id = matches[0].lower()

            function_name = (
                "plugin_geticon_"
                + plugin_id
            )

            body = extract_function_body(
                source,
                function_name,
            )

            return {
                "id": plugin_id,
                "source_file": candidate,
                "source": source,
                "icon_function": function_name,
                "icon_body": body,
            }

    return {
        "id": repo.lower(),
        "source_file": "",
        "source": "",
        "icon_function": "",
        "icon_body": "",
    }


def resolve_runtime_icon(
    function_body,
    paths,
):
    if not function_body:
        return ""

    repository_paths = {
        path.lower(): path
        for path in paths
    }

    patterns = (
        r"/plugins/[A-Za-z0-9_.-]+/"
        r"(images/[A-Za-z0-9_./ -]+\."
        r"(?:png|jpe?g|gif|svg|webp))",

        r"['\"]"
        r"(images/[A-Za-z0-9_./ -]+\."
        r"(?:png|jpe?g|gif|svg|webp))"
        r"['\"]",

        r"['\"]"
        r"(admin/images/[A-Za-z0-9_./ -]+\."
        r"(?:png|jpe?g|gif|svg|webp))"
        r"['\"]",

        r"['\"]"
        r"(public_html/[A-Za-z0-9_./ -]+\."
        r"(?:png|jpe?g|gif|svg|webp))"
        r"['\"]",
    )

    found = []

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            function_body,
            re.I,
        ):
            path = match.group(1)

            path = path.replace(
                "\\",
                "/",
            ).lstrip("/")

            found.append(path)

    checked = set()

    for relative_path in found:
        candidates = [
            relative_path,
        ]

        if relative_path.startswith(
            "images/"
        ):
            candidates.insert(
                0,
                "admin/" + relative_path,
            )

            candidates.append(
                "public_html/" + relative_path
            )

        for candidate in candidates:
            key = candidate.lower()

            if key in checked:
                continue

            checked.add(key)

            if key in repository_paths:
                return repository_paths[key]

    return ""


def image_candidates(
    paths,
    plugin_id,
    repo_name,
):
    preferred_names = {
        plugin_id.lower(),
        repo_name.lower(),
        normalize(plugin_id),
        normalize(repo_name),
    }

    ranked = []

    for path in paths:
        low = path.lower()

        if not low.endswith(
            IMAGE_EXTS
        ):
            continue

        filename = low.rsplit(
            "/",
            1,
        )[-1]

        stem = filename.rsplit(
            ".",
            1,
        )[0]

        score = 100

        if low.startswith(
            "admin/images/"
        ):
            score -= 40

        elif low.startswith(
            "public_html/images/"
        ):
            score -= 25

        elif low.startswith(
            "images/"
        ):
            score -= 25

        elif "/images/" in low:
            score -= 15

        if stem in preferred_names:
            score -= 45

        elif normalize(stem) in preferred_names:
            score -= 35

        suspicious_terms = (
            "snap",
            "screenshot",
            "preview",
            "banner",
            "header",
            "background",
            "sample",
            "demo",
        )

        for term in suspicious_terms:
            if term in filename:
                score += 50

        ranked.append(
            (
                score,
                path,
            )
        )

    ranked.sort(
        key=lambda item: (
            item[0],
            item[1].lower(),
        )
    )

    return [
        path
        for score, path in ranked
    ]


def valid_existing_manifest(text):
    try:
        data = json.loads(text)

    except Exception:
        return False, None

    if not isinstance(
        data,
        dict,
    ):
        return False, data

    if data.get("schema") != 1:
        return False, data

    plugin_id = data.get("id")

    if (
        not isinstance(plugin_id, str)
        or not plugin_id.strip()
    ):
        return False, data

    name = data.get("name")

    if (
        not isinstance(name, str)
        or not name.strip()
    ):
        return False, data

    icon = data.get("icon")

    if icon is not None:
        if (
            not isinstance(icon, str)
            or not icon.strip()
        ):
            return False, data

        if icon.startswith("/"):
            return False, data

        parts = icon.replace(
            "\\",
            "/",
        ).split("/")

        if ".." in parts:
            return False, data

    return True, data


def create_manifest(
    plugin_id,
    repo_name,
    icon,
):
    data = {
        "schema": 1,
        "id": plugin_id,
        "name": repo_name,
        "icon": icon,
    }

    return json.dumps(
        data,
        indent=2,
        ensure_ascii=False,
    ) + "\n"


def branch_exists(
    gh,
    org,
    repo,
    branch,
):
    encoded_branch = urllib.parse.quote(
        branch,
        safe="",
    )

    try:
        gh.get(
            "/repos/%s/%s/git/ref/heads/%s"
            % (
                org,
                repo,
                encoded_branch,
            )
        )

        return True

    except RuntimeError as exc:
        if "404" in str(exc):
            return False

        raise


def get_open_metadata_pr(
    gh,
    org,
    repo,
):
    head = urllib.parse.quote(
        "%s:%s"
        % (
            org,
            PR_BRANCH,
        ),
        safe=":",
    )

    pulls = gh.get(
        "/repos/%s/%s/pulls"
        "?state=open&head=%s"
        % (
            org,
            repo,
            head,
        )
    )

    if pulls:
        return pulls[0]

    return None


def open_pr_for_manifest(
    gh,
    org,
    repo,
    default_branch,
    manifest_text,
):
    existing_pr = get_open_metadata_pr(
        gh,
        org,
        repo,
    )

    if existing_pr:
        return existing_pr[
            "html_url"
        ]

    if branch_exists(
        gh,
        org,
        repo,
        PR_BRANCH,
    ):
        raise RuntimeError(
            "Branch %s already exists without an open PR; "
            "review or delete that branch before retrying"
            % PR_BRANCH
        )

    encoded_default_branch = (
        urllib.parse.quote(
            default_branch,
            safe="",
        )
    )

    base = gh.get(
        "/repos/%s/%s/git/ref/heads/%s"
        % (
            org,
            repo,
            encoded_default_branch,
        )
    )

    sha = base[
        "object"
    ][
        "sha"
    ]

    gh.post(
        "/repos/%s/%s/git/refs"
        % (
            org,
            repo,
        ),
        {
            "ref": (
                "refs/heads/"
                + PR_BRANCH
            ),
            "sha": sha,
        },
    )

    encoded_manifest = (
        base64.b64encode(
            manifest_text.encode(
                "utf-8"
            )
        ).decode(
            "ascii"
        )
    )

    gh.put(
        "/repos/%s/%s/contents/plugin.json"
        % (
            org,
            repo,
        ),
        {
            "message": (
                "Add static plugin metadata manifest"
            ),
            "content": encoded_manifest,
            "branch": PR_BRANCH,
        },
    )

    pr = gh.post(
        "/repos/%s/%s/pulls"
        % (
            org,
            repo,
        ),
        {
            "title": (
                "Add static plugin metadata manifest"
            ),
            "head": PR_BRANCH,
            "base": default_branch,
            "body": (
                "Adds `plugin.json` using the "
                "Geeklog plugin metadata convention.\n\n"
                "The declared icon was confirmed from "
                "the existing `plugin_geticon_*()` "
                "runtime callback and references an "
                "existing repository asset.\n\n"
                "No executable plugin code is changed."
            ),
        },
    )

    return pr[
        "html_url"
    ]


def repository_selected(
    repo_name,
    requested,
):
    if requested.lower() == "all":
        return True

    return (
        repo_name.lower()
        == requested.lower()
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Audit Geeklog plugin metadata "
            "and optionally open safe manifest PRs."
        )
    )

    parser.add_argument(
        "--org",
        default=os.getenv(
            "PLUGIN_METADATA_ORG",
            "Geeklog-Plugins",
        ),
    )

    parser.add_argument(
        "--mode",
        choices=(
            "audit",
            "pr",
        ),
        default=os.getenv(
            "PLUGIN_METADATA_MODE",
            "audit",
        ),
    )

    parser.add_argument(
        "--repository",
        default=os.getenv(
            "PLUGIN_METADATA_REPOSITORY",
            "all",
        ),
        help=(
            "Repository name to process, "
            "or 'all'."
        ),
    )

    parser.add_argument(
        "--report",
        default=(
            "PLUGIN_METADATA_REPORT.md"
        ),
    )

    args = parser.parse_args()

    read_token = os.getenv(
        "GITHUB_TOKEN",
        "",
    )

    write_token = os.getenv(
        "PLUGIN_METADATA_TOKEN",
        "",
    )

    token = read_token

    if args.mode == "pr":
        if not write_token:
            print(
                "ERROR: PLUGIN_METADATA_TOKEN "
                "is required in pr mode.",
                file=sys.stderr,
            )

            return 2

        token = write_token

    gh = GitHub(token)

    rows = []
    selected_found = False

    repositories = paginate(
        gh,
        "/orgs/%s/repos?type=all"
        % args.org,
    )

    for repo in repositories:
        name = repo[
            "name"
        ]

        if not repository_selected(
            name,
            args.repository,
        ):
            continue

        selected_found = True

        if (
            name in DEFAULT_EXCLUDES
            or repo.get("archived")
            or repo.get("fork")
        ):
            rows.append(
                {
                    "repo": name,
                    "status": "SKIPPED",
                    "confidence": "-",
                    "id": "",
                    "icon": "",
                    "action": (
                        "excluded, archived or fork"
                    ),
                }
            )

            continue

        default_branch = repo[
            "default_branch"
        ]

        encoded_branch = (
            urllib.parse.quote(
                default_branch,
                safe="",
            )
        )

        tree = gh.get(
            "/repos/%s/%s/git/trees/%s"
            "?recursive=1"
            % (
                args.org,
                name,
                encoded_branch,
            )
        )

        paths = [
            item["path"]
            for item in tree.get(
                "tree",
                [],
            )
            if item.get("type") == "blob"
        ]

        path_set = set(paths)

        if "plugin.json" in path_set:
            text = fetch_text(
                gh,
                args.org,
                name,
                "plugin.json",
                default_branch,
            )

            valid, data = (
                valid_existing_manifest(
                    text
                )
            )

            rows.append(
                {
                    "repo": name,
                    "status": (
                        "OK"
                        if valid
                        else "REVIEW"
                    ),
                    "confidence": (
                        "existing"
                        if valid
                        else "-"
                    ),
                    "id": (
                        data.get(
                            "id",
                            "",
                        )
                        if isinstance(
                            data,
                            dict,
                        )
                        else ""
                    ),
                    "icon": (
                        data.get(
                            "icon",
                            "",
                        )
                        if isinstance(
                            data,
                            dict,
                        )
                        else ""
                    ),
                    "action": (
                        "existing manifest"
                        if valid
                        else (
                            "invalid plugin.json"
                        )
                    ),
                }
            )

            continue

        identity = find_plugin_identity(
            gh,
            args.org,
            name,
            default_branch,
            path_set,
        )

        plugin_id = identity[
            "id"
        ]

        confirmed_icon = (
            resolve_runtime_icon(
                identity[
                    "icon_body"
                ],
                paths,
            )
        )

        confidence = ""
        icon = ""

        if confirmed_icon:
            confidence = (
                "runtime callback"
            )

            icon = confirmed_icon

            status = "CONFIRMED"

        else:
            candidates = (
                image_candidates(
                    paths,
                    plugin_id,
                    name,
                )
            )

            if candidates:
                confidence = (
                    "heuristic"
                )

                icon = candidates[0]

                status = "CANDIDATE"

            else:
                status = "REVIEW"
                confidence = "-"
                icon = ""

        action = "audit only"

        if args.mode == "pr":
            if status != "CONFIRMED":
                action = (
                    "PR not created: "
                    "only CONFIRMED icons "
                    "are eligible"
                )

            else:
                manifest = (
                    create_manifest(
                        plugin_id,
                        name,
                        icon,
                    )
                )

                try:
                    pr_url = (
                        open_pr_for_manifest(
                            gh,
                            args.org,
                            name,
                            default_branch,
                            manifest,
                        )
                    )

                    action = pr_url
                    status = "PR"

                except Exception as exc:
                    status = "ERROR"

                    action = str(exc)

        rows.append(
            {
                "repo": name,
                "status": status,
                "confidence": confidence,
                "id": plugin_id,
                "icon": icon,
                "action": action,
            }
        )

    if (
        args.repository.lower()
        != "all"
        and not selected_found
    ):
        print(
            "ERROR: repository '%s' was not found "
            "in organization '%s'."
            % (
                args.repository,
                args.org,
            ),
            file=sys.stderr,
        )

        return 3

    rows.sort(
        key=lambda row: (
            row[
                "repo"
            ].lower()
        )
    )

    lines = [
        "# Geeklog plugin metadata audit",
        "",
        "Mode: `%s`" % args.mode,
        "",
        "Repository filter: `%s`"
        % args.repository,
        "",
        (
            "| Repository | Status | "
            "Confidence | Plugin id | "
            "Icon | Action |"
        ),
        (
            "|---|---|---|---|---|---|"
        ),
    ]

    for row in rows:
        def cell(value):
            return (
                str(value)
                .replace(
                    "|",
                    "\\|",
                )
                .replace(
                    "\n",
                    " ",
                )
            )

        lines.append(
            "| %s | %s | %s | `%s` | `%s` | %s |"
            % (
                cell(
                    row["repo"]
                ),
                cell(
                    row["status"]
                ),
                cell(
                    row["confidence"]
                ),
                cell(
                    row["id"]
                ),
                cell(
                    row["icon"]
                ),
                cell(
                    row["action"]
                ),
            )
        )

    counts = {}

    for row in rows:
        status = row[
            "status"
        ]

        counts[
            status
        ] = (
            counts.get(
                status,
                0,
            )
            + 1
        )

    lines.extend(
        [
            "",
            "## Summary",
            "",
        ]
    )

    for status in sorted(
        counts.keys()
    ):
        lines.append(
            "- `%s`: %d"
            % (
                status,
                counts[
                    status
                ],
            )
        )

    with open(
        args.report,
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(
            "\n".join(
                lines
            )
            + "\n"
        )

    print(
        json.dumps(
            counts,
            sort_keys=True,
        )
    )

    if counts.get(
        "ERROR"
    ):
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(
        main()
    )
