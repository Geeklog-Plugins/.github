#!/usr/bin/env python3

import argparse
import os
import re
import subprocess
import sys
import tempfile


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


def parse_confirmed_repositories(report_text):
    repositories = []

    for line in report_text.splitlines():
        if not line.startswith("|"):
            continue

        cells = [
            cell.strip()
            for cell in line.strip().strip("|").split("|")
        ]

        if len(cells) < 8:
            continue

        repository = cells[0]
        status = cells[1]

        if repository in ("Repository", "---"):
            continue

        if (
            status == "CONFIRMED"
            and re.match(
                r"^[A-Za-z0-9_.-]+$",
                repository,
            )
        ):
            repositories.append(
                repository
            )

    return repositories


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Create safe plugin metadata PRs "
            "for CONFIRMED repositories only."
        )
    )

    parser.add_argument(
        "--org",
        required=True,
    )

    parser.add_argument(
        "--report",
        default="PLUGIN_METADATA_REPORT.md",
    )

    parser.add_argument(
        "--max-prs",
        type=int,
        default=15,
    )

    args = parser.parse_args()

    if args.max_prs < 1:
        print(
            "ERROR: --max-prs must be at least 1.",
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
            "ERROR: plugin_metadata.py was not found.",
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
                audit_text = handle.read()

        except OSError as exc:
            print(
                "ERROR: could not read audit report: %s"
                % exc,
                file=sys.stderr,
            )
            return 2

        confirmed = (
            parse_confirmed_repositories(
                audit_text
            )
        )

        selected = confirmed[
            :args.max_prs
        ]

        deferred = confirmed[
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
                with open(
                    repo_report,
                    "r",
                    encoding="utf-8",
                ) as handle:
                    for line in handle:
                        if not line.startswith(
                            "|"
                        ):
                            continue

                        cells = [
                            cell.strip()
                            for cell in (
                                line.strip()
                                .strip("|")
                                .split("|")
                            )
                        ]

                        if (
                            len(cells) >= 8
                            and cells[0]
                            == repository
                        ):
                            status = cells[1]
                            action = cells[7]
                            break

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
            "# Geeklog plugin metadata batch PR report",
            "",
            "Mode: `pr`",
            "",
            "Repository filter: `all`",
            "",
            (
                "Only repositories classified as "
                "`CONFIRMED` by the initial audit "
                "are eligible."
            ),
            "",
            (
                "Maximum PR attempts for this run: `%d`"
                % args.max_prs
            ),
            "",
            "| Repository | Result | Action |",
            "|---|---|---|",
        ]

        for repository, status, action in results:
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

        lines.extend(
            [
                "",
                "## Summary",
                "",
                (
                    "- `CONFIRMED found`: %d"
                    % len(confirmed)
                ),
                (
                    "- `PR attempts`: %d"
                    % len(selected)
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

        with open(
            args.report,
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write(
                "\n".join(lines)
                + "\n"
            )

        return (
            1
            if failures
            else 0
        )


if __name__ == "__main__":
    sys.exit(
        main()
    )
