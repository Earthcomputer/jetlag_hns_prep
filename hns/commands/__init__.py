"""One module per subcommand, each providing register(subparsers) and run(args)."""

from hns.commands import (
    contour,
    copy_layer,
    delete_layers,
    fetch_places,
    hide_layers,
    list_fields,
    list_layers,
    split_layer,
    voronoi,
)

# In the order they appear in --help
COMMANDS = [list_layers, voronoi, list_fields, split_layer, contour, hide_layers, delete_layers, copy_layer, fetch_places]
