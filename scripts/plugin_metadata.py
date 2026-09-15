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

PLUGIN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+){1,3}$")


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
                return json.loads(raw) if raw else None

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
        return self.request("GET", path)

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
    if (
        not isinstance(obj, dict)
        or obj.get("encoding") != "base64"
    ):
        return ""

    try:
        return base64.b64decode(
            obj.get("content", "")
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

    match = pattern.search(source)

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
                return source[start:position]

        position += 1

    return ""


def find_plugin_identity(
    gh,
    org,
    repo,
    branch,
    paths,
):
    plugin_id = ""
    id_source = ""
    icon_function = ""
    icon_body = ""

    id_patterns = (
        re.compile(
            r"""['"]pi_name['"]\s*=>\s*['"]([A-Za-z0-9_.-]+)['"]""",
            re.I,
        ),
        re.compile(
            r"""\$pi_name\s*=\s*['"]([A-Za-z0-9_.-]+)['"]\s*;""",
            re.I,
        ),
    )

    for candidate in (
        "autoinstall.php",
        "install.php",
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

        if not plugin_id:
            for pattern in id_patterns:
                match = pattern.search(source)

                if match:
                    candidate_id = match.group(1).strip()

                    if PLUGIN_ID_RE.match(candidate_id):
                        plugin_id = candidate_id.lower()
                        id_source = candidate + ":pi_name"
                        break

        if candidate in (
            "functions.inc",
            "api.inc",
        ):
            matches = re.findall(
                r"function\s+plugin_geticon_"
                r"([A-Za-z0-9_]+)\s*\(",
                source,
                re.I,
            )

            if matches:
                callback_id = matches[0].lower()
                callback = (
                    "plugin_geticon_"
                    + callback_id
                )

                if not plugin_id:
                    plugin_id = callback_id
                    id_source = candidate + ":plugin_geticon"

                if callback_id == plugin_id:
                    icon_function = callback
                    icon_body = extract_function_body(
                        source,
                        callback,
                    )

        if (
            plugin_id
            and id_source.endswith(":pi_name")
            and icon_body
        ):
            break

    if plugin_id:
        return {
            "id": plugin_id,
            "id_source": id_source,
            "confirmed": True,
            "icon_function": icon_function,
            "icon_body": icon_body,
        }

    fallback_id = repo.lower()

    if not PLUGIN_ID_RE.match(fallback_id):
        fallback_id = re.sub(
            r"[^a-z0-9_.-]+",
            "-",
            fallback_id,
        ).strip("-")

    return {
        "id": fallback_id,
        "id_source": "repository fallback",
        "confirmed": False,
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

        match = pattern.search(source)

        if not match:
            continue

        if match.group(1) is not None:
            display_name = decode_php_single_quoted(
                match.group(1)
            )
        else:
            display_name = decode_php_double_quoted(
                match.group(2)
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
            found.append(relative_path)

    checked = set()

    for relative_path in found:
        candidates = [relative_path]

        if relative_path.startswith("images/"):
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

        if not low.endswith(IMAGE_EXTS):
            continue

        filename = low.rsplit("/", 1)[-1]
        stem = filename.rsplit(".", 1)[0]
        score = 100

        if low.startswith("admin/images/"):
            score -= 40
        elif low.startswith("public_html/images/"):
            score -= 25
        elif low.startswith("images/"):
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


def normalize_requirement_version(value):
    if not isinstance(value, str):
        return ""

    value = value.strip()

    if value.lower().startswith("v"):
        value = value[1:]

    if not VERSION_RE.match(value):
        return ""

    return value


def find_geeklog_requirement(
    gh,
    org,
    repo,
    branch,
    paths,
):
    candidates = (
        "autoinstall.php",
        "install.php",
        "functions.inc",
    )

    patterns = (
        re.compile(
            r"""['"]pi_gl_version['"]\s*=>\s*['"]([0-9]+(?:\.[0-9]+){1,3})['"]""",
            re.I,
        ),
        re.compile(
            r"""\$pi_gl_version\s*=\s*['"]([0-9]+(?:\.[0-9]+){1,3})['"]\s*;""",
            re.I,
        ),
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

        for pattern in patterns:
            match = pattern.search(source)

            if match:
                version = normalize_requirement_version(
                    match.group(1)
                )

                if version:
                    return {
                        "version": version,
                        "source": candidate + ":pi_gl_version",
                        "confidence": "confirmed",
                    }

    return {
        "version": "",
        "source": "",
        "confidence": "unknown",
    }


def find_php_requirement(
    gh,
    org,
    repo,
    branch,
    paths,
):
    candidates = (
        "autoinstall.php",
        "functions.inc",
        "install.php",
        "README.md",
    )

    version_compare_pattern = re.compile(
        r"""version_compare\s*\(\s*PHP_VERSION\s*,\s*['"]([0-9]+(?:\.[0-9]+){1,3})['"]\s*,\s*['"]<['"]\s*\)""",
        re.I,
    )

    php_version_id_pattern = re.compile(
        r"""PHP_VERSION_ID\s*<\s*([0-9]{5,6})""",
        re.I,
    )

    textual_patterns = (
        re.compile(
            r"""(?:requires?|minimum|minimum\s+php|php\s+minimum|php)\s*[:>= ]+\s*PHP?\s*([0-9]+(?:\.[0-9]+){1,3})""",
            re.I,
        ),
        re.compile(
            r"""PHP\s+([0-9]+(?:\.[0-9]+){1,3})\s*(?:or newer|or later|and newer|\+|minimum|min)""",
            re.I,
        ),
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

        match = version_compare_pattern.search(source)

        if match:
            version = normalize_requirement_version(
                match.group(1)
            )

            if version:
                return {
                    "version": version,
                    "source": candidate + ":version_compare(PHP_VERSION)",
                    "confidence": "confirmed",
                }

        match = php_version_id_pattern.search(source)

        if match:
            version_id = int(match.group(1))
            major = version_id // 10000
            minor = (version_id % 10000) // 100
            patch = version_id % 100
            version = "%d.%d.%d" % (
                major,
                minor,
                patch,
            )

            return {
                "version": version,
                "source": candidate + ":PHP_VERSION_ID",
                "confidence": "confirmed",
            }

        if candidate == "README.md":
            for pattern in textual_patterns:
                match = pattern.search(source)

                if match:
                    version = normalize_requirement_version(
                        match.group(1)
                    )

                    if version:
                        return {
                            "version": version,
                            "source": candidate + ":text",
                            "confidence": "candidate",
                        }

    return {
        "version": "",
        "source": "",
        "confidence": "unknown",
    }


def find_requirements(
    gh,
    org,
    repo,
    branch,
    paths,
):
    geeklog = find_geeklog_requirement(
        gh,
        org,
        repo,
        branch,
        paths,
    )
    php = find_php_requirement(
        gh,
        org,
        repo,
        branch,
        paths,
    )

    return {
        "geeklog": geeklog,
        "php": php,
    }


def manifest_requirements(data):
    result = {
        "geeklog": "",
        "php": "",
    }

    if not isinstance(data, dict):
        return result

    requires = data.get("requires")

    if not isinstance(requires, dict):
        return result

    for key in result:
        value = requires.get(key)

        if isinstance(value, str):
            result[key] = value.strip()

    return result


def validate_manifest_data(
    data,
    paths=None,
):
    if not isinstance(data, dict):
        return (
            False,
            "manifest is not a JSON object",
        )

    if data.get("schema") != 1:
        return (
            False,
            "schema must be 1",
        )

    plugin_id = data.get("id")

    if (
        not isinstance(plugin_id, str)
        or not plugin_id.strip()
        or not PLUGIN_ID_RE.match(plugin_id)
    ):
        return (
            False,
            "invalid plugin id",
        )

    name = data.get("name")

    if (
        not isinstance(name, str)
        or not name.strip()
    ):
        return (
            False,
            "invalid plugin name",
        )

    icon = data.get("icon")

    if icon is not None:
        if (
            not isinstance(icon, str)
            or not icon.strip()
        ):
            return (
                False,
                "invalid icon",
            )

        normalized_icon = icon.replace(
            "\\",
            "/",
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

    requires = data.get("requires")

    if requires is not None:
        if not isinstance(requires, dict):
            return (
                False,
                "requires must be a JSON object",
            )

        allowed = {
            "geeklog",
            "php",
        }

        for key in requires:
            if key not in allowed:
                return (
                    False,
                    "unknown requires field: %s"
                    % key,
                )

        for key in allowed:
            if key not in requires:
                continue

            value = requires[key]

            if (
                not isinstance(value, str)
                or not normalize_requirement_version(
                    value
                )
            ):
                return (
                    False,
                    "invalid requires.%s version"
                    % key,
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
        data = json.loads(text)

    except Exception:
        return (
            False,
            None,
            "invalid JSON",
        )

    valid, reason = validate_manifest_data(
        data,
        paths,
    )

    return (
        valid,
        data,
        reason,
    )


def merge_detected_requirements(
    data,
    requirements,
):
    merged = dict(data)
    current = manifest_requirements(merged)
    detected = {}
    sources = {}

    for kind in (
        "geeklog",
        "php",
    ):
        info = requirements.get(kind, {})
        version = normalize_requirement_version(
            info.get("version", "")
        )

        if (
            not version
            or info.get("confidence") != "confirmed"
        ):
            continue

        detected[kind] = version
        sources[kind] = info.get(
            "source",
            "",
        )

    if not detected:
        return (
            merged,
            False,
            sources,
        )

    requires = merged.get("requires")

    if not isinstance(requires, dict):
        requires = {}
    else:
        requires = dict(requires)

    changed = False

    for kind, version in detected.items():
        existing = current.get(
            kind,
            "",
        )

        if existing != version:
            requires[kind] = version
            changed = True

    if requires:
        merged["requires"] = requires

    return (
        merged,
        changed,
        sources,
    )


def create_manifest(
    plugin_id,
    plugin_name,
    icon,
    requirements=None,
):
    data = {
        "schema": 1,
        "id": plugin_id,
        "name": plugin_name,
    }

    if icon:
        data["icon"] = icon

    if requirements:
        requires = {}

        for kind in (
            "geeklog",
            "php",
        ):
            info = requirements.get(
                kind,
                {},
            )
            version = normalize_requirement_version(
                info.get(
                    "version",
                    "",
                )
            )

            if (
                version
                and info.get("confidence") == "confirmed"
            ):
                requires[kind] = version

        if requires:
            data["requires"] = requires

    valid, reason = validate_manifest_data(data)

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


def serialize_manifest(data):
    valid, reason = validate_manifest_data(data)

    if not valid:
        raise RuntimeError(
            (
                "Refusing to serialize "
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
    encoded_branch = urllib.parse.quote(
        branch,
        safe="",
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
    encoded_manifest = base64.b64encode(
        manifest_text.encode("utf-8")
    ).decode("ascii")

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
    encoded_manifest = base64.b64encode(
        manifest_text.encode("utf-8")
    ).decode("ascii")

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
                "Update static plugin "
                "metadata manifest"
            ),
            "head": PR_BRANCH,
            "base": default_branch,
            "body": (
                "Adds or enriches `plugin.json` "
                "using the Geeklog plugin metadata "
                "convention.\n\n"
                "Where available, the plugin name "
                "is read from the English language "
                "file. When a reliable icon can "
                "be resolved it is included, but the "
                "icon is optional. The Geeklog "
                "minimum version is read from "
                "`pi_gl_version`, and the PHP minimum "
                "version is read from an explicit "
                "runtime compatibility check.\n\n"
                "Requirements are only added when "
                "they can be detected with sufficient "
                "confidence. No executable plugin "
                "code is changed."
            ),
        },
    )

    return pr["html_url"]


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
        plugin_json = fetch_optional_content(
            gh,
            org,
            repo,
            "plugin.json",
            PR_BRANCH,
        )

        if plugin_json is None:
            create_plugin_json_on_branch(
                gh,
                org,
                repo,
                manifest_text,
            )

            return existing_pr["html_url"]

        existing_text = decode_content(
            plugin_json
        )

        if existing_text == manifest_text:
            return existing_pr["html_url"]

        current_sha = plugin_json.get("sha")

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

        return existing_pr["html_url"]

    encoded_default_branch = urllib.parse.quote(
        default_branch,
        safe="",
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

        plugin_json = fetch_optional_content(
            gh,
            org,
            repo,
            "plugin.json",
            PR_BRANCH,
        )

        if plugin_json is None:
            create_plugin_json_on_branch(
                gh,
                org,
                repo,
                manifest_text,
            )
        else:
            current_sha = plugin_json.get("sha")

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

    branch_sha = existing_branch[
        "object"
    ][
        "sha"
    ]

    plugin_json = fetch_optional_content(
        gh,
        org,
        repo,
        "plugin.json",
        PR_BRANCH,
    )

    if plugin_json is not None:
        existing_text = decode_content(
            plugin_json
        )

        if existing_text == manifest_text:
            return create_metadata_pr(
                gh,
                org,
                repo,
                default_branch,
            )

        current_sha = plugin_json.get("sha")

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


def requirement_report_value(info):
    version = info.get(
        "version",
        "",
    )

    if not version:
        return ""

    confidence = info.get(
        "confidence",
        "",
    )
    source = info.get(
        "source",
        "",
    )

    parts = [version]

    if confidence:
        parts.append(confidence)

    if source:
        parts.append(source)

    return " / ".join(parts)


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
        default="PLUGIN_METADATA_REPORT.md",
    )

    args = parser.parse_args()

    if (
        args.mode == "pr"
        and args.repository.lower() == "all"
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

    gh = GitHub(token)
    rows = []
    selected_found = False

    repositories = paginate(
        gh,
        "/orgs/%s/repos?type=all"
        % args.org,
    )

    for repo in repositories:
        repo_name = repo["name"]

        if not repository_selected(
            repo_name,
            args.repository,
        ):
            continue

        selected_found = True

        if (
            repo_name in DEFAULT_EXCLUDES
            or repo.get("archived")
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
                    "geeklog": "",
                    "php": "",
                    "action": (
                        "excluded or archived"
                    ),
                }
            )
            continue

        default_branch = repo["default_branch"]

        encoded_branch = urllib.parse.quote(
            default_branch,
            safe="",
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

        if tree.get("truncated"):
            rows.append(
                {
                    "repo": repo_name,
                    "status": "REVIEW",
                    "confidence": "-",
                    "id": "",
                    "name": "",
                    "name_source": "-",
                    "icon": "",
                    "geeklog": "",
                    "php": "",
                    "action": (
                        "repository tree was "
                        "truncated by GitHub API"
                    ),
                }
            )
            continue

        paths = [
            item["path"]
            for item in tree.get(
                "tree",
                [],
            )
            if item.get("type") == "blob"
        ]

        path_set = set(paths)

        requirements = find_requirements(
            gh,
            args.org,
            repo_name,
            default_branch,
            path_set,
        )

        if "plugin.json" in path_set:
            text = fetch_text(
                gh,
                args.org,
                repo_name,
                "plugin.json",
                default_branch,
            )

            valid, data, reason = valid_existing_manifest(
                text,
                path_set,
            )

            if not valid:
                rows.append(
                    {
                        "repo": repo_name,
                        "status": "REVIEW",
                        "confidence": "-",
                        "id": (
                            data.get("id", "")
                            if isinstance(data, dict)
                            else ""
                        ),
                        "name": (
                            data.get("name", "")
                            if isinstance(data, dict)
                            else ""
                        ),
                        "name_source": "-",
                        "icon": (
                            data.get("icon", "")
                            if isinstance(data, dict)
                            else ""
                        ),
                        "geeklog": requirement_report_value(
                            requirements["geeklog"]
                        ),
                        "php": requirement_report_value(
                            requirements["php"]
                        ),
                        "action": (
                            "invalid plugin.json: "
                            + reason
                        ),
                    }
                )
                continue

            enriched, changed, sources = (
                merge_detected_requirements(
                    data,
                    requirements,
                )
            )

            current_requires = manifest_requirements(
                data
            )
            final_requires = manifest_requirements(
                enriched
            )

            status = "ENRICH" if changed else "OK"
            action = (
                "detected requirements can enrich "
                "existing manifest"
                if changed
                else "existing manifest"
            )

            if args.mode == "pr" and changed:
                manifest = serialize_manifest(
                    enriched
                )

                try:
                    action = open_pr_for_manifest(
                        gh,
                        args.org,
                        repo_name,
                        default_branch,
                        manifest,
                    )
                    status = "PR"

                except Exception as exc:
                    status = "ERROR"
                    action = str(exc)

            confidence = "existing"

            if changed:
                confidence = (
                    "existing + detected requirements"
                )

            rows.append(
                {
                    "repo": repo_name,
                    "status": status,
                    "confidence": confidence,
                    "id": data.get("id", ""),
                    "name": data.get("name", ""),
                    "name_source": "plugin.json",
                    "icon": data.get("icon", ""),
                    "geeklog": (
                        final_requires.get(
                            "geeklog",
                            "",
                        )
                        or current_requires.get(
                            "geeklog",
                            "",
                        )
                    ),
                    "php": (
                        final_requires.get(
                            "php",
                            "",
                        )
                        or current_requires.get(
                            "php",
                            "",
                        )
                    ),
                    "action": action,
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

        plugin_id = identity["id"]

        plugin_name, name_source = (
            find_plugin_display_name(
                gh,
                args.org,
                repo_name,
                default_branch,
                path_set,
            )
        )

        confirmed_icon = resolve_runtime_icon(
            identity["icon_body"],
            paths,
        )

        icon = confirmed_icon

        if not icon:
            candidates = image_candidates(
                paths,
                plugin_id,
                repo_name,
            )

            if candidates:
                icon = candidates[0]

        identity_confirmed = bool(
            identity.get("confirmed")
        )
        name_confirmed = (
            name_source.startswith("language/")
        )

        if identity_confirmed:
            if not name_confirmed:
                plugin_name = plugin_id
                name_source = "plugin id fallback"

            status = "CONFIRMED"
            confidence = (
                identity.get(
                    "id_source",
                    "confirmed identity",
                )
                + (
                    " + english plugin_name"
                    if name_confirmed
                    else " + plugin id fallback"
                )
            )
        else:
            status = "CANDIDATE"
            confidence = (
                "repository-name id fallback"
                + (
                    " + english plugin_name"
                    if name_confirmed
                    else " + repository-name fallback"
                )
            )

        if status == "CANDIDATE":
            action = (
                "audit only; manual "
                "plugin id review required"
            )
        elif icon:
            action = "audit only"
        else:
            action = (
                "audit only; icon unavailable "
                "(icon is optional)"
            )

        if args.mode == "pr":
            if status != "CONFIRMED":
                action = (
                    "PR not created: only "
                    "CONFIRMED metadata is eligible"
                )
            else:
                manifest = create_manifest(
                    plugin_id,
                    plugin_name,
                    icon,
                    requirements,
                )

                try:
                    action = open_pr_for_manifest(
                        gh,
                        args.org,
                        repo_name,
                        default_branch,
                        manifest,
                    )
                    status = "PR"

                except Exception as exc:
                    status = "ERROR"
                    action = str(exc)

        rows.append(
            {
                "repo": repo_name,
                "status": status,
                "confidence": confidence,
                "id": plugin_id,
                "name": plugin_name,
                "name_source": name_source,
                "icon": icon,
                "geeklog": requirement_report_value(
                    requirements["geeklog"]
                ),
                "php": requirement_report_value(
                    requirements["php"]
                ),
                "action": action,
            }
        )

    if (
        args.repository.lower() != "all"
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
        key=lambda row: row["repo"].lower()
    )

    lines = [
        "# Geeklog plugin metadata audit",
        "",
        "Mode: `%s`" % args.mode,
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
            "Icon | Geeklog min | PHP min | "
            "Action |"
        ),
        (
            "|---|---|---|---|---|---|---|---|---|---|"
        ),
    ]

    for row in rows:
        lines.append(
            (
                "| %s | %s | %s | `%s` | "
                "%s | %s | `%s` | %s | %s | %s |"
            )
            % (
                safe_cell(row["repo"]),
                safe_cell(row["status"]),
                safe_cell(row["confidence"]),
                safe_cell(row["id"]),
                safe_cell(row["name"]),
                safe_cell(row["name_source"]),
                safe_cell(row["icon"]),
                safe_cell(row["geeklog"]),
                safe_cell(row["php"]),
                safe_cell(row["action"]),
            )
        )

    counts = {}

    for row in rows:
        status = row["status"]

        counts[status] = (
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

    for status in sorted(counts):
        lines.append(
            "- `%s`: %d"
            % (
                status,
                counts[status],
            )
        )

    with open(
        args.report,
        "w",
        encoding="utf-8",
    ) as handle:
        handle.write(
            "\n".join(lines)
            + "\n"
        )

    print(
        json.dumps(
            counts,
            sort_keys=True,
        )
    )

    if counts.get("ERROR"):
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
