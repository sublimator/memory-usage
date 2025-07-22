#!/usr/bin/env python3
"""
Stage files with formatting and optionally commit them.

This script formats Python code using isort and black,
then stages the specified files with git add, and optionally commits them.

Usage:
    python scripts/stage.py [files...] [--commit-message "Your commit message"]

If no files are specified, it will format and stage all changed and untracked Python files.
Files that have already been deleted but not yet staged are recognized and their deletions are staged.
This script will skip files that have already been staged for deletion.
"""

import argparse
import os
import subprocess
import sys


def run_command(command, description=None):
    """Run a shell command and print output."""
    if description:
        print(f"Running {description}...")

    try:
        result = subprocess.run(command, check=True, text=True, capture_output=True)

        if result.stdout:
            print(result.stdout)

        return True
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")
        if e.stdout:
            print(e.stdout)
        if e.stderr:
            print(e.stderr)
        return False


def get_git_changed_files():
    """Get all changed and untracked Python files from git, separating deleted files."""
    # Get modified tracked files
    result = subprocess.run(
        ["git", "diff", "--name-only"], check=True, text=True, capture_output=True
    )
    modified_files = [f for f in result.stdout.strip().split("\n") if f]

    # Get staged but not committed files
    result = subprocess.run(
        ["git", "diff", "--staged", "--name-only"],
        check=True,
        text=True,
        capture_output=True,
    )
    staged_files = [f for f in result.stdout.strip().split("\n") if f]

    # Get untracked files
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        check=True,
        text=True,
        capture_output=True,
    )
    untracked_files = [f for f in result.stdout.strip().split("\n") if f]

    # Get deleted files that aren't staged yet
    # These are files that exist in the index but are missing in the working tree
    result = subprocess.run(
        ["git", "ls-files", "--deleted"],
        check=True,
        text=True,
        capture_output=True,
    )
    deleted_files_unstaged = [f for f in result.stdout.strip().split("\n") if f]

    # Get staged deleted files - we'll exclude these to avoid trying to stage them again
    result = subprocess.run(
        ["git", "diff", "--staged", "--diff-filter=D", "--name-only"],
        check=True,
        text=True,
        capture_output=True,
    )
    deleted_files_staged = [f for f in result.stdout.strip().split("\n") if f]

    # Only include deleted files that aren't already staged
    deleted_files = [f for f in deleted_files_unstaged if f not in deleted_files_staged]
    deleted_python_files = [f for f in deleted_files if f.endswith(".py")]

    # Combine all modified/new files and filter out deleted files
    all_modified_files = list(set(modified_files + staged_files + untracked_files))
    # Only include files that exist
    existing_files = [f for f in all_modified_files if os.path.exists(f)]
    # Filter for Python files
    python_files = [f for f in existing_files if f.endswith(".py")]

    return python_files, deleted_python_files


def stage_and_commit(files=".", commit_message=None):
    """Run formatting tools, stage files, and optionally commit them.

    Args:
        files: Files or directories to format and stage.
              Defaults to current directory (.)
        commit_message: Optional commit message. If provided, changes will be committed.
    """
    # Get files to process
    if files == "." or (isinstance(files, list) and len(files) == 0):
        # Get all changed and untracked Python files, separating deleted files
        python_files, deleted_python_files = get_git_changed_files()
    else:
        # When specific files are provided, check if they exist
        python_files = [f for f in files if f.endswith(".py") and os.path.exists(f)]
        # For custom file lists, we don't automatically handle deletions
        deleted_python_files = []

    if not python_files and not deleted_python_files:
        print("No Python files to process")
        return True

    # Print files to be processed
    if python_files:
        print(f"Formatting and staging {len(python_files)} Python files:")
        for file in python_files:
            print(f"  {file}")

    if deleted_python_files:
        print(
            f"Found {len(deleted_python_files)} unstaged deleted Python files (will stage the deletions):"
        )
        for file in deleted_python_files:
            print(f"  {file}")

    # Only run formatters on existing files
    if python_files:
        # Run isort on specific files
        isort_cmd = ["poetry", "run", "isort"] + python_files
        if not run_command(isort_cmd, "isort"):
            return False

        # Run black on specific files
        black_cmd = ["poetry", "run", "black"] + python_files
        if not run_command(black_cmd, "black"):
            return False

        # Stage modified files with git
        git_command = ["git", "add"] + python_files
        if not run_command(git_command, "git add for modified files"):
            return False

    # Stage deleted files separately (this stages the deletion, doesn't delete the files)
    if deleted_python_files:
        print(f"Found {len(deleted_python_files)} deleted Python files that need staging:")
        for deleted_file in deleted_python_files:
            print(f"  {deleted_file}")
            # Simple git add will stage the deletion without removing any files
            git_command = ["git", "add", deleted_file]
            if not run_command(git_command, f"Staging deletion for {deleted_file}"):
                return False

    # Commit changes if a message was provided
    if commit_message:
        commit_cmd = ["git", "commit", "-m", commit_message]
        if not run_command(commit_cmd, "git commit"):
            return False
        print(f"Changes committed with message: '{commit_message}'")

    return True


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description="Format Python code and stage/commit changes")
    parser.add_argument(
        "files",
        nargs="*",
        default=".",
        help="Files or directories to format (default: changed Python files)",
    )
    parser.add_argument(
        "-m",
        "--commit-message",
        help="Commit message (if provided, changes will be committed)",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()

    success = stage_and_commit(args.files, args.commit_message)

    if success:
        if args.commit_message:
            print("Files formatted, staged, and committed successfully!")
        else:
            print("Files formatted and staged successfully!")
    else:
        print("Error occurred during processing.")
        sys.exit(1)
