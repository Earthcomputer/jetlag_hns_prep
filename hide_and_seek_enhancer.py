#!/usr/bin/env python3
"""
hide_and_seek_enhancer.py

Tools for preparing Hide and Seek game maps stored as KML or KMZ files, where each
Folder is a layer. KMZ input is read from its doc.kml, and KMZ output keeps the
input's other files (e.g. icons). Each subcommand lives in hns/commands/.

Usage:
    # List all layers in a KML or KMZ file
    python hide_and_seek_enhancer.py list input_points.kml
    python hide_and_seek_enhancer.py list input_points.kmz

    # List the features (names, locations, styles) within a layer
    python hide_and_seek_enhancer.py list-features input.kml "LayerName"

    # Generate Voronoi diagrams for all layers (preserves existing layers)
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml
    python hide_and_seek_enhancer.py voronoi input_points.kmz output_voronoi.kmz

    # Generate Voronoi diagrams excluding specific layers
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --exclude-layers Layer1,Layer2

    # Generate with custom clip extent
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --min-lon -2.0 --min-lat 51.0 --max-lon -1.0 --max-lat 52.0

    # List the data fields of a layer that split-layer can split by
    python hide_and_seek_enhancer.py list-fields input.kml "LayerName"

    # Split a layer by type into separate layers
    python hide_and_seek_enhancer.py split-layer input.kml output_split.kml "LayerName"

    # Generate contour lines around all layers (point, line, polygon geometries)
    python hide_and_seek_enhancer.py contour input.kml output_contours.kml \
        --max-distance-km 50

    # Generate contours with secondary intervals for close distances
    python hide_and_seek_enhancer.py contour input.kml output_contours.kml \
        --step-km 10 --secondary-step-km 2 --secondary-threshold-km 10 \
        --max-distance-km 50

    # Add elevation contour lines every 10 m (darker every 50 m) over the extent of a layer,
    # using free elevation data from AWS Terrain Tiles
    python hide_and_seek_enhancer.py elevation input.kml output_elevation.kml \
        --area-layer "game boandaries" --step-m 10 --major-step-m 50

    # Hide all layers except some
    python hide_and_seek_enhancer.py hide-layers input.kml output.kml \
        --exclude-layers Layer1,Layer2

    # Delete layers from a KML or KMZ file
    python hide_and_seek_enhancer.py delete-layers input.kml output.kml \
        --layers Layer1,Layer2

    # Rename a layer
    python hide_and_seek_enhancer.py rename-layer input.kml output.kml "OldName" "NewName"

    # Sort layers alphabetically, with some layers kept first and some last
    python hide_and_seek_enhancer.py sort-layers input.kml output.kml \
        --first Layer1,Layer2 --last Layer3

    # Only move some layers to the start or end, keeping the order of the others
    python hide_and_seek_enhancer.py sort-layers input.kml output.kml \
        --first Layer1 --last Layer3 --keep

    # Copy features from one layer to another existing layer
    python hide_and_seek_enhancer.py copy-layer input.kml output.kml \
        --from-layer SourceLayer --to-layer TargetLayer

    # Add places from Google Maps as a new layer (API key in google_maps_api_key.txt)
    python hide_and_seek_enhancer.py fetch-places input.kml output.kml \
        --type park --max-results 100 \
        --min-lon -2.0 --min-lat 51.0 --max-lon -1.0 --max-lat 52.0

Dependencies:
    pip install geopandas scipy shapely pyproj fiona lxml requests contourpy pillow
"""

from hns.cli import main

if __name__ == "__main__":
    main()
