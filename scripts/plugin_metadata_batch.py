#!/usr/bin/env python3

import argparse
import os
import re
import subprocess
import sys
import tempfile


ELIGIBLE_STATUSES = (
    "CONFIRMED",
    "ENRICH",
)

REPOSITORY_RE = re.compile(
    r"^[A-Za-z0-9_.-]+$"
)


def run_command(args, env=None):
    process = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    return (
        process.returncode,
        process.stdout,
        process.stderr,
    )


def split_markdown_row(line):
    if not line.startswith("|"):
        return []

    return [
        cell.strip()
        for cell in (
            line.strip()
            .strip("|")
            .split("|")
        )
    ]


def markdown_table_rows(report_text):
    """
    Parse the first Markdown table whose header contains Repository and Status.

    Rows are returned as dictionaries keyed by the actual column names so the
    batch script does not depend on fixed column indexes.
    """
    headers = None
    rows = []

    for line in report_text.splitlines():
        if not line.startswith("|"):
            if headers is not None and rows:
                break
            continue

        cells = split_markdown_row(line)

        if not cells:
            continue

        if headers is None:
            if (
                "Repository" in cells
                and "Status" in cells
            ):
                headers = cells
            continue

        # Skip Markdown separator row.
        if all(
            re.match(r"^:?-{3,}:?$", cell)
            for cell in cells
        ):
            continue

        # Ignore malformed rows instead of guessing.
        if len(cells) != len(headers):
            continue

        rows.append(
            dict(
                zip(
                    headers,
                    cells,
                )
            )
        )

    return rows


def parse_eligible_repositories(report_text):
    repositories = []

    for row in markdown_table_rows(
        report_text
    ):
        repository = row.get(
            "Repository",
            "",
        ).strip()

        status = row.get(
            "Status",
            "",
        ).strip()

        if (
            status in ELIGIBLE_STATUSES
            and REPOSITORY_RE.match(
                repository
            )
        ):
            repositories.append(
                repository
            )

    return repositories


def parse_repository_result(
    report_text,
    repository,
):
    """
    Return (status, action) for one repository from a per-repository report.
    """
    for row in markdown_table_rows(
        report_text
    ):
        if (
            row.get(
                "Repository",
                "",
            ).strip()
            != repository
        ):
            continue

        return (
            row.get(
                "Status",
                "",
            ).strip(),
            row.get(
                "Action",
                "",
            ).strip(),
        )

    return (
        "",
        "",
    )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create safe Geeklog plugin metadata PRs "
            "for repositories classified as "
            "CONFIRMED or ENRICH by the initial audit."
        )
    )

    parser.add_argument(
        "--org",
        required=True,
        help=(
            "GitHub organization containing "
            "the plugin repositories."
        ),
    )

    parser.add_argument(
        "--report",
        default=(
            "PLUGIN_METADATA_BATCH_REPORT.md"
        ),
        help=(
            "Path of the final batch report."
        ),
    )

    parser.add_argument(
        "--max-prs",
        type=int,
        default=15,
        help=(
            "Maximum number of PR attempts "
            "during this run."
        ),
    )

    args = parser.parse_args()

    if args.max_prs < 1:
        print(
            (
                "ERROR: --max-prs must "
                "be at least 1."
            ),
            file=sys.stderr,
        )
        return 2

    token = os.getenv(
        "PLUGIN_METADATA_TOKEN",
        "",
    )

    if not token:
        print(
            (
                "ERROR: PLUGIN_METADATA_TOKEN "
                "is required for batch PR mode."
            ),
            file=sys.stderr,
        )
        return 2

    script = os.path.join(
        os.path.dirname(__file__),
        "plugin_metadata.py",
    )

    if not os.path.isfile(script):
        print(
            (
                "ERROR: plugin_metadata.py "
                "was not found."
            ),
            file=sys.stderr,
        )
        return 2

    with tempfile.TemporaryDirectory() as temp_dir:
        audit_report = os.path.join(
            temp_dir,
            "audit.md",
        )

        audit_env = os.environ.copy()
        audit_env[
            "PLUGIN_METADATA_MODE"
        ] = "audit"
        audit_env[
            "PLUGIN_METADATA_REPOSITORY"
        ] = "all"

        code, out, err = run_command(
            [
                sys.executable,
                script,
                "--org",
                args.org,
                "--mode",
                "audit",
                "--repository",
                "all",
                "--report",
                audit_report,
            ],
            env=audit_env,
        )

        if out:
            print(
                out,
                end="",
            )

        if err:
            print(
                err,
                end="",
                file=sys.stderr,
            )

        if code != 0:
            print(
                (
                    "ERROR: global audit failed; "
                    "no PRs were attempted."
                ),
                file=sys.stderr,
            )
            return code

        try:
            with open(
                audit_report,
                "r",
                encoding="utf-8",
            ) as handle:
                audit_text = (
                    handle.read()
                )

        except OSError as exc:
            print(
                (
                    "ERROR: could not read "
                    "audit report: %s"
                )
                % exc,
                file=sys.stderr,
            )
            return 2

        eligible = (
            parse_eligible_repositories(
                audit_text
            )
        )

        selected = eligible[
            :args.max_prs
        ]

        deferred = eligible[
            args.max_prs:
        ]

        results = []

        for repository in selected:
            repo_report = os.path.join(
                temp_dir,
                "%s.md" % repository,
            )

            repo_env = (
                os.environ.copy()
            )

            repo_env[
                "PLUGIN_METADATA_MODE"
            ] = "pr"

            repo_env[
                "PLUGIN_METADATA_REPOSITORY"
            ] = repository

            code, out, err = run_command(
                [
                    sys.executable,
                    script,
                    "--org",
                    args.org,
                    "--mode",
                    "pr",
                    "--repository",
                    repository,
                    "--report",
                    repo_report,
                ],
                env=repo_env,
            )

            if out:
                print(
                    "[%s] %s"
                    % (
                        repository,
                        out.strip(),
                    )
                )

            if err:
                print(
                    "[%s] %s"
                    % (
                        repository,
                        err.strip(),
                    ),
                    file=sys.stderr,
                )

            status = (
                "ERROR"
                if code != 0
                else "DONE"
            )

            action = ""

            if os.path.isfile(
                repo_report
            ):
                try:
                    with open(
                        repo_report,
                        "r",
                        encoding="utf-8",
                    ) as handle:
                        report_text = (
                            handle.read()
                        )

                    parsed_status, parsed_action = (
                        parse_repository_result(
                            report_text,
                            repository,
                        )
                    )

                    if parsed_status:
                        status = parsed_status

                    if parsed_action:
                        action = parsed_action

                except OSError as exc:
                    if not action:
                        action = (
                            "could not read "
                            "repository report: %s"
                            % exc
                        )

            if (
                code != 0
                and not action
            ):
                action = (
                    "repository PR run failed"
                )

            results.append(
                (
                    repository,
                    status,
                    action,
                )
            )

        lines = [
            (
                "# Geeklog plugin metadata "
                "batch PR report"
            ),
            "",
            "Mode: `pr`",
            "",
            (
                "Repository filter: `all`"
            ),
            "",
            (
                "Repositories classified as "
                "`CONFIRMED` or `ENRICH` "
                "by the initial audit are eligible."
            ),
            "",
            (
                "Maximum PR attempts for "
                "this run: `%d`"
                % args.max_prs
            ),
            "",
            (
                "| Repository | Result | "
                "Action |"
            ),
            "|---|---|---|",
        ]

        for (
            repository,
            status,
            action,
        ) in results:
            lines.append(
                "| %s | %s | %s |"
                % (
                    repository,
                    status,
                    action.replace(
                        "|",
                        "\\|",
                    ),
                )
            )

        for repository in deferred:
            lines.append(
                (
                    "| %s | SKIPPED | "
                    "PR limit reached |"
                )
                % repository
            )

        failures = [
            row
            for row in results
            if row[1] == "ERROR"
        ]

        successful = [
            row
            for row in results
            if row[1] in (
                "PR",
                "DONE",
            )
        ]

        lines.extend(
            [
                "",
                "## Summary",
                "",
                (
                    "- `Eligible found`: %d"
                    % len(eligible)
                ),
                (
                    "- `PR attempts`: %d"
                    % len(selected)
                ),
                (
                    "- `Successful runs`: %d"
                    % len(successful)
                ),
                (
                    "- `Deferred by limit`: %d"
                    % len(deferred)
                ),
                (
                    "- `Failures`: %d"
                    % len(failures)
                ),
            ]
        )

        try:
            with open(
                args.report,
                "w",
                encoding="utf-8",
            ) as handle:
                handle.write(
                    "\n".join(lines)
                    + "\n"
                )

        except OSError as exc:
            print(
                (
                    "ERROR: could not write "
                    "batch report: %s"
                )
                % exc,
                file=sys.stderr,
            )
            return 2

        return (
            1
            if failures
            else 0
        )


if __name__ == "__main__":
    sys.exit(
        main()
    )
