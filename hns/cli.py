"""Command-line entry point dispatching to the subcommands."""

import argparse

from hns.commands import COMMANDS


def main(argv: list[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Prepare Hide and Seek game maps: manage KML/KMZ layers and add Voronoi diagrams, contours and places."
    )
    subparsers = parser.add_subparsers(dest="command", help="Command to execute")
    for command in COMMANDS:
        command.register(subparsers)

    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return
    args.func(args)
