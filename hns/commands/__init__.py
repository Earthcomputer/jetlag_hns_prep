"""One module per subcommand, each providing register(subparsers) and run(args)."""

from hns.commands import (
    contour,
    copy_layer,
    delete_layers,
    fetch_places,
    hide_layers,
    list_features,
    list_fields,
    list_layers,
    rename_layer,
    split_layer,
    voronoi,
)

# In the order they appear in --help
COMMANDS = [list_layers, list_features, voronoi, list_fields, split_layer, contour, hide_layers, delete_layers, rename_layer, copy_layer, fetch_places]
