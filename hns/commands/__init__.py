"""One module per subcommand, each providing register(subparsers) and run(args)."""

from hns.commands import (
    contour,
    copy_layer,
    delete_layers,
    elevation,
    fetch_places,
    fetch_routes,
    hide_layers,
    list_features,
    list_fields,
    list_layers,
    merge_layers,
    rename_layer,
    sort_layers,
    split_layer,
    voronoi,
)

# In the order they appear in --help
COMMANDS = [list_layers, list_features, voronoi, list_fields, split_layer, contour, elevation, hide_layers, delete_layers, rename_layer, sort_layers, copy_layer, merge_layers, fetch_places, fetch_routes]
