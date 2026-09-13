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

PLUGIN_ID_RE = re.compile(
    r"^[A-Za-z0-9_.-]+$"
)


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None):
        url = (
            path
            if path.startswith("https://")
            else API + path
        )

        data = None

        if payload is not None:
            data = json.dumps(
                payload
            ).encode("utf-8")

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
                raw = response.read().decode(
                    "utf-8"
                )

                return (
                    json.loads(raw)
                    if raw
                    else None
                )

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
        separator = (
            "&"
            if "?" in path
            else "?"
        )

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
    if (
        not isinstance(obj, dict)
        or obj.get("encoding") != "base64"
    ):
        return ""

    try:
        return base64.b64decode(
            obj.get(
                "content",
                "",
            )
        ).decode(
            "utf-8",
            "replace",
        )

    except Exception:
        return ""


def fetch_content_object(
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

    return gh.get(
        "/repos/%s/%s/contents/%s?ref=%s"
        % (
            org,
            repo,
            quoted_path,
            quoted_ref,
        )
    )


def fetch_optional_content(
    gh,
    org,
    repo,
    path,
    ref,
):
    try:
        return fetch_content_object(
            gh,
            org,
            repo,
            path,
            ref,
        )

    except RuntimeError as exc:
        if "404" in str(exc):
            return None

        raise


def fetch_text(
    gh,
    org,
    repo,
    path,
    ref,
):
    return decode_content(
        fetch_content_object(
            gh,
            org,
            repo,
            path,
            ref,
        )
    )


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

    match = pattern.search(
        source
    )

    if not match:
        return ""

    start = match.end()

    depth = 1
    position = start
    quote = None
    escaped = False

    while position < len(source):
        char = source[position]

        if quote is not None:
            if escaped:
                escaped = False

            elif char == "\\":
                escaped = True

            elif char == quote:
                quote = None

            position += 1
            continue

        if char in ("'", '"'):
            quote = char

        elif char == "{":
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
    for candidate in (
        "functions.inc",
        "api.inc",
    ):
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
            plugin_id = (
                matches[0].lower()
            )

            function_name = (
                "plugin_geticon_"
                + plugin_id
            )

            return {
                "id": plugin_id,
                "source_file": candidate,
                "icon_function": function_name,
                "icon_body": extract_function_body(
                    source,
                    function_name,
                ),
            }

    fallback_id = repo.lower()

    if not PLUGIN_ID_RE.match(
        fallback_id
    ):
        fallback_id = re.sub(
            r"[^a-z0-9_.-]+",
            "-",
            fallback_id,
        ).strip("-")

    return {
        "id": fallback_id,
        "source_file": "",
        "icon_function": "",
        "icon_body": "",
    }


def decode_php_single_quoted(value):
    return (
        value
        .replace("\\'", "'")
        .replace("\\\\", "\\")
    )


def decode_php_double_quoted(value):
    return (
        value
        .replace('\\"', '"')
        .replace("\\\\", "\\")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\t", "\t")
    )


def find_plugin_display_name(
    gh,
    org,
    repo,
    branch,
    paths,
):
    candidates = (
        "language/english.php",
        "language/english_utf-8.php",
        "language/english_utf8.php",
    )

    pattern = re.compile(
        r"""['"]plugin_name['"]\s*=>\s*(?:'((?:\\.|[^'\\])*)'|"((?:\\.|[^"\\])*)")""",
        re.I,
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

        match = pattern.search(
            source
        )

        if not match:
            continue

        if match.group(1) is not None:
            display_name = (
                decode_php_single_quoted(
                    match.group(1)
                )
            )

        else:
            display_name = (
                decode_php_double_quoted(
                    match.group(2)
                )
            )

        display_name = " ".join(
            display_name.split()
        ).strip()

        if display_name:
            return (
                display_name,
                candidate,
            )

    return (
        repo,
        "repository fallback",
    )


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
        (
            r"/plugins/[A-Za-z0-9_.-]+/"
            r"(images/[A-Za-z0-9_./ -]+"
            r"\.(?:png|jpe?g|gif|svg|webp))"
        ),
        (
            r"""['"](images/[A-Za-z0-9_./ -]+\.(?:png|jpe?g|gif|svg|webp))['"]"""
        ),
        (
            r"""['"](admin/images/[A-Za-z0-9_./ -]+\.(?:png|jpe?g|gif|svg|webp))['"]"""
        ),
        (
            r"""['"](public_html/[A-Za-z0-9_./ -]+\.(?:png|jpe?g|gif|svg|webp))['"]"""
        ),
    )

    found = []

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            function_body,
            re.I,
        ):
            relative_path = (
                match.group(1)
                .replace("\\", "/")
                .strip()
                .lstrip("/")
            )

            found.append(
                relative_path
            )

    checked = set()

    for relative_path in found:
        candidates = [
            relative_path
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

            checked.add(
                key
            )

            if key in repository_paths:
                return repository_paths[
                    key
                ]

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

    suspicious_terms = (
        "snap",
        "screenshot",
        "preview",
        "banner",
        "header",
        "background",
        "sample",
        "demo",
        "thumb",
        "thumbnail",
    )

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
        for _, path in ranked
    ]


def validate_manifest_data(
    data,
    paths=None,
):
    if not isinstance(
        data,
        dict,
    ):
        return (
            False,
            "manifest is not a JSON object",
        )

    if data.get(
        "schema"
    ) != 1:
        return (
            False,
            "schema must be 1",
        )

    plugin_id = data.get(
        "id"
    )

    if (
        not isinstance(
            plugin_id,
            str,
        )
        or not plugin_id.strip()
        or not PLUGIN_ID_RE.match(
            plugin_id
        )
    ):
        return (
            False,
            "invalid plugin id",
        )

    name = data.get(
        "name"
    )

    if (
        not isinstance(
            name,
            str,
        )
        or not name.strip()
    ):
        return (
            False,
            "invalid plugin name",
        )

    icon = data.get(
        "icon"
    )

    if icon is not None:
        if (
            not isinstance(
                icon,
                str,
            )
            or not icon.strip()
        ):
            return (
                False,
                "invalid icon",
            )

        normalized_icon = (
            icon.replace("\\", "/")
        )

        if (
            normalized_icon.startswith("/")
            or "://" in normalized_icon
            or normalized_icon.startswith("//")
            or ".." in normalized_icon.split("/")
        ):
            return (
                False,
                (
                    "icon must be a safe "
                    "repository-relative path"
                ),
            )

        if not normalized_icon.lower().endswith(
            IMAGE_EXTS
        ):
            return (
                False,
                (
                    "icon is not a supported "
                    "image type"
                ),
            )

        if (
            paths is not None
            and normalized_icon not in paths
        ):
            return (
                False,
                (
                    "icon path does not exist "
                    "in repository"
                ),
            )

    return (
        True,
        "",
    )


def valid_existing_manifest(
    text,
    paths=None,
):
    try:
        data = json.loads(
            text
        )

    except Exception:
        return (
            False,
            None,
            "invalid JSON",
        )

    valid, reason = (
        validate_manifest_data(
            data,
            paths,
        )
    )

    return (
        valid,
        data,
        reason,
    )


def create_manifest(
    plugin_id,
    plugin_name,
    icon,
):
    data = {
        "schema": 1,
        "id": plugin_id,
        "name": plugin_name,
        "icon": icon,
    }

    valid, reason = (
        validate_manifest_data(
            data
        )
    )

    if not valid:
        raise RuntimeError(
            (
                "Refusing to create "
                "invalid plugin.json: "
            )
            + reason
        )

    return (
        json.dumps(
            data,
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )


def branch_ref(
    gh,
    org,
    repo,
    branch,
):
    encoded_branch = (
        urllib.parse.quote(
            branch,
            safe="",
        )
    )

    try:
        return gh.get(
            "/repos/%s/%s/git/ref/heads/%s"
            % (
                org,
                repo,
                encoded_branch,
            )
        )

    except RuntimeError as exc:
        if "404" in str(exc):
            return None

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
        (
            "/repos/%s/%s/pulls"
            "?state=open&head=%s"
        )
        % (
            org,
            repo,
            head,
        )
    )

    if pulls:
        return pulls[0]

    return None


def create_plugin_json_on_branch(
    gh,
    org,
    repo,
    manifest_text,
):
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
                "Add static plugin "
                "metadata manifest"
            ),
            "content": encoded_manifest,
            "branch": PR_BRANCH,
        },
    )


def update_plugin_json_on_branch(
    gh,
    org,
    repo,
    manifest_text,
    current_sha,
):
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
                "Update static plugin "
                "metadata manifest"
            ),
            "content": encoded_manifest,
            "branch": PR_BRANCH,
            "sha": current_sha,
        },
    )


def create_metadata_pr(
    gh,
    org,
    repo,
    default_branch,
):
    pr = gh.post(
        "/repos/%s/%s/pulls"
        % (
            org,
            repo,
        ),
        {
            "title": (
                "Add static plugin "
                "metadata manifest"
            ),
            "head": PR_BRANCH,
            "base": default_branch,
            "body": (
                "Adds `plugin.json` using the "
                "Geeklog plugin metadata convention."
                "\n\n"
                "The plugin name was read from "
                "the English language file and "
                "the icon was confirmed from the "
                "existing `plugin_geticon_*()` "
                "runtime callback."
                "\n\n"
                "No executable plugin code is changed."
            ),
        },
    )

    return pr[
        "html_url"
    ]


def open_pr_for_manifest(
    gh,
    org,
    repo,
    default_branch,
    manifest_text,
):
    existing_pr = (
        get_open_metadata_pr(
            gh,
            org,
            repo,
        )
    )

    if existing_pr:
        plugin_json = (
            fetch_optional_content(
                gh,
                org,
                repo,
                "plugin.json",
                PR_BRANCH,
            )
        )

        if plugin_json is None:
            create_plugin_json_on_branch(
                gh,
                org,
                repo,
                manifest_text,
            )

            return existing_pr[
                "html_url"
            ]

        existing_text = (
            decode_content(
                plugin_json
            )
        )

        if existing_text == manifest_text:
            return existing_pr[
                "html_url"
            ]

        current_sha = (
            plugin_json.get(
                "sha"
            )
        )

        if not current_sha:
            raise RuntimeError(
                (
                    "Existing plugin.json "
                    "has no blob SHA; "
                    "cannot update safely"
                )
            )

        update_plugin_json_on_branch(
            gh,
            org,
            repo,
            manifest_text,
            current_sha,
        )

        return existing_pr[
            "html_url"
        ]

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

    base_sha = base[
        "object"
    ][
        "sha"
    ]

    existing_branch = branch_ref(
        gh,
        org,
        repo,
        PR_BRANCH,
    )

    if existing_branch is None:
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
                "sha": base_sha,
            },
        )

        create_plugin_json_on_branch(
            gh,
            org,
            repo,
            manifest_text,
        )

        return create_metadata_pr(
            gh,
            org,
            repo,
            default_branch,
        )

    branch_sha = (
        existing_branch[
            "object"
        ][
            "sha"
        ]
    )

    plugin_json = (
        fetch_optional_content(
            gh,
            org,
            repo,
            "plugin.json",
            PR_BRANCH,
        )
    )

    if plugin_json is not None:
        existing_text = (
            decode_content(
                plugin_json
            )
        )

        if existing_text == manifest_text:
            return create_metadata_pr(
                gh,
                org,
                repo,
                default_branch,
            )

        current_sha = (
            plugin_json.get(
                "sha"
            )
        )

        if not current_sha:
            raise RuntimeError(
                (
                    "Existing plugin.json "
                    "has no blob SHA; "
                    "cannot update safely"
                )
            )

        update_plugin_json_on_branch(
            gh,
            org,
            repo,
            manifest_text,
            current_sha,
        )

        return create_metadata_pr(
            gh,
            org,
            repo,
            default_branch,
        )

    if branch_sha != base_sha:
        raise RuntimeError(
            (
                "Branch %s already exists, "
                "has no plugin.json, and differs "
                "from the current base branch; "
                "manual review required"
            )
            % PR_BRANCH
        )

    create_plugin_json_on_branch(
        gh,
        org,
        repo,
        manifest_text,
    )

    return create_metadata_pr(
        gh,
        org,
        repo,
        default_branch,
    )


def repository_selected(
    repo_name,
    requested,
):
    return (
        requested.lower() == "all"
        or repo_name.lower()
        == requested.lower()
    )


def safe_cell(value):
    return (
        str(value)
        .replace("|", "\\|")
        .replace("\n", " ")
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Audit Geeklog plugin metadata "
            "and optionally open safe "
            "plugin.json pull requests."
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

    if (
        args.mode == "pr"
        and args.repository.lower()
        == "all"
    ):
        print(
            (
                "ERROR: PR mode requires one "
                "explicit repository; 'all' "
                "is intentionally disabled."
            ),
            file=sys.stderr,
        )

        return 2

    read_token = os.getenv(
        "GITHUB_TOKEN",
        "",
    )

    write_token = os.getenv(
        "PLUGIN_METADATA_TOKEN",
        "",
    )

    if args.mode == "pr":
        if not write_token:
            print(
                (
                    "ERROR: PLUGIN_METADATA_TOKEN "
                    "is required in pr mode."
                ),
                file=sys.stderr,
            )

            return 2

        token = write_token

    else:
        token = read_token

    gh = GitHub(
        token
    )

    rows = []
    selected_found = False

    repositories = paginate(
        gh,
        "/orgs/%s/repos?type=all"
        % args.org,
    )

    for repo in repositories:
        repo_name = repo[
            "name"
        ]

        if not repository_selected(
            repo_name,
            args.repository,
        ):
            continue

        selected_found = True

        if (
            repo_name in DEFAULT_EXCLUDES
            or repo.get("archived")
            or repo.get("fork")
        ):
            rows.append(
                {
                    "repo": repo_name,
                    "status": "SKIPPED",
                    "confidence": "-",
                    "id": "",
                    "name": "",
                    "name_source": "-",
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
            (
                "/repos/%s/%s/git/trees/%s"
                "?recursive=1"
            )
            % (
                args.org,
                repo_name,
                encoded_branch,
            )
        )

        if tree.get(
            "truncated"
        ):
            rows.append(
                {
                    "repo": repo_name,
                    "status": "REVIEW",
                    "confidence": "-",
                    "id": "",
                    "name": "",
                    "name_source": "-",
                    "icon": "",
                    "action": (
                        "repository tree was "
                        "truncated by GitHub API"
                    ),
                }
            )

            continue

        paths = [
            item[
                "path"
            ]
            for item in tree.get(
                "tree",
                [],
            )
            if item.get(
                "type"
            ) == "blob"
        ]

        path_set = set(
            paths
        )

        if "plugin.json" in path_set:
            text = fetch_text(
                gh,
                args.org,
                repo_name,
                "plugin.json",
                default_branch,
            )

            valid, data, reason = (
                valid_existing_manifest(
                    text,
                    path_set,
                )
            )

            rows.append(
                {
                    "repo": repo_name,
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
                    "name": (
                        data.get(
                            "name",
                            "",
                        )
                        if isinstance(
                            data,
                            dict,
                        )
                        else ""
                    ),
                    "name_source": (
                        "plugin.json"
                        if valid
                        else "-"
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
                            "invalid plugin.json: "
                            + reason
                        )
                    ),
                }
            )

            continue

        identity = find_plugin_identity(
            gh,
            args.org,
            repo_name,
            default_branch,
            path_set,
        )

        plugin_id = identity[
            "id"
        ]

        plugin_name, name_source = (
            find_plugin_display_name(
                gh,
                args.org,
                repo_name,
                default_branch,
                path_set,
            )
        )

        confirmed_icon = (
            resolve_runtime_icon(
                identity[
                    "icon_body"
                ],
                paths,
            )
        )

        if confirmed_icon:
            icon = confirmed_icon

            if name_source.startswith(
                "language/"
            ):
                status = (
                    "CONFIRMED"
                )

                confidence = (
                    "runtime callback + "
                    "english plugin_name"
                )

            else:
                status = (
                    "CANDIDATE"
                )

                confidence = (
                    "runtime callback + "
                    "repository-name fallback"
                )

        else:
            candidates = (
                image_candidates(
                    paths,
                    plugin_id,
                    repo_name,
                )
            )

            if candidates:
                icon = candidates[
                    0
                ]

                status = (
                    "CANDIDATE"
                )

                confidence = (
                    "heuristic icon"
                )

            else:
                icon = ""

                status = (
                    "REVIEW"
                )

                confidence = "-"

        if status == "REVIEW":
            action = (
                "no reliable icon found"
            )

        elif status == "CANDIDATE":
            action = (
                "audit only; manual "
                "review required"
            )

        else:
            action = (
                "audit only"
            )

        if args.mode == "pr":
            if status != "CONFIRMED":
                action = (
                    "PR not created: only "
                    "CONFIRMED metadata is eligible"
                )

            else:
                manifest = (
                    create_manifest(
                        plugin_id,
                        plugin_name,
                        icon,
                    )
                )

                try:
                    action = (
                        open_pr_for_manifest(
                            gh,
                            args.org,
                            repo_name,
                            default_branch,
                            manifest,
                        )
                    )

                    status = "PR"

                except Exception as exc:
                    status = (
                        "ERROR"
                    )

                    action = str(
                        exc
                    )

        rows.append(
            {
                "repo": repo_name,
                "status": status,
                "confidence": confidence,
                "id": plugin_id,
                "name": plugin_name,
                "name_source": name_source,
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
            (
                "ERROR: repository '%s' "
                "was not found in "
                "organization '%s'."
            )
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
        "Mode: `%s`"
        % args.mode,
        "",
        (
            "Repository filter: `%s`"
            % args.repository
        ),
        "",
        (
            "| Repository | Status | "
            "Confidence | Plugin id | "
            "Plugin name | Name source | "
            "Icon | Action |"
        ),
        (
            "|---|---|---|---|---|---|---|---|"
        ),
    ]

    for row in rows:
        lines.append(
            (
                "| %s | %s | %s | `%s` | "
                "%s | %s | `%s` | %s |"
            )
            % (
                safe_cell(
                    row["repo"]
                ),
                safe_cell(
                    row["status"]
                ),
                safe_cell(
                    row["confidence"]
                ),
                safe_cell(
                    row["id"]
                ),
                safe_cell(
                    row["name"]
                ),
                safe_cell(
                    row[
                        "name_source"
                    ]
                ),
                safe_cell(
                    row["icon"]
                ),
                safe_cell(
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
        counts
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
