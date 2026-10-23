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

    # Or only for specific layers (also works for contour)
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --include-layers Layer1,Layer2

    # Generate with custom clip extent
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --min-lon -2.0 --min-lat 51.0 --max-lon -1.0 --max-lat 52.0

    # Or clip to the extent of a layer's features (works wherever --min-lon etc. do)
    python hide_and_seek_enhancer.py voronoi input_points.kml output_voronoi.kml \
        --area-layer "Game Area"

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

    # Merge two layers into one, in the position of the first (optionally renaming it)
    python hide_and_seek_enhancer.py merge-layers input.kml output.kml "Layer1" "Layer2" \
        --name "Merged Layer"

    # Add places from Google Maps as a new layer (API key in google_maps_api_key.txt)
    python hide_and_seek_enhancer.py fetch-places input.kml output.kml \
        --type park --max-results 100 \
        --min-lon -2.0 --min-lat 51.0 --max-lon -1.0 --max-lat 52.0

    # Add the bus routes running on a given date from an operator's GTFS timetable feed
    # (here Nottingham City Transport), drawn thicker the more journeys start between 10:00 and 16:00
    python hide_and_seek_enhancer.py fetch-routes input.kml output.kml \
        --gtfs https://data.discoverpassenger.com/operator/nctx/dataset/current/download/gtfs \
        --agency NCT --date 2026-10-24 --frequency-window 10:00-16:00 \
        --layer-name "NCT Bus Routes" --name-prefix "NCT "

    # The same, drawn straight through roundabouts, with routes sharing a road side by side
    # and their numbers labelled every 1.5 km in a "Labels" sublayer beside the "Lines"
    python hide_and_seek_enhancer.py fetch-routes input.kml output.kml \
        --gtfs https://data.discoverpassenger.com/operator/nctx/dataset/current/download/gtfs \
        --agency NCT --date 2026-10-24 --frequency-window 10:00-16:00 \
        --layer-name "NCT Bus Routes" --name-prefix "NCT " --collapse-roundabouts --side-by-side --label-every-m 1500

    # Combine several operators from several feeds (here those taking the Robin Hood day ticket, from
    # NCT's feed and the Bus Open Data Service's East Midlands feed), laid out together with one layer each.
    # Routes are clipped to the game area, and trips without shapes are drawn along OpenStreetMap roads,
    # read from a local extract (https://download.geofabrik.de) rather than the slow Overpass API
    python hide_and_seek_enhancer.py fetch-routes input.kml output.kml \
        --gtfs nct_gtfs.zip --gtfs https://data.bus-data.dft.gov.uk/timetable/download/gtfs-file/east_midlands/ \
        --agency "NCT=NCT,trentbarton,CT4N,Kinchbus,Stagecoach East Midlands=Stagecoach,OP202=NottsBus Connect" \
        --exclude-routes "trentbarton=RA,SNX,3N" --only-routes "Stagecoach=PRO,SA,141" \
        --date 2026-10-24 --frequency-window 10:00-16:00 \
        --layer-name "{agency} Bus Routes" --name-prefix "{agency} " --clip-layer "Game Boundaries" \
        --osm-file nottinghamshire-latest.osm.pbf --collapse-roundabouts --side-by-side --label-every-m 1500

    # Add routes from OpenStreetMap route relations with the given tags (here the Nottingham tram),
    # with their width set from a known frequency (every 10 minutes) on the same scale
    python hide_and_seek_enhancer.py fetch-routes input.kml output.kml \
        --osm-tags "route=tram,operator=Tramlink Nottingham" --services-per-hour 6 \
        --layer-name "NET Tram Routes" --name-prefix "NET Line " \
        --side-by-side --label-every-m 1500 --label-prefix T

    # Train routes of two operators (a tag may list alternatives with |), named by the stations they run between
    python hide_and_seek_enhancer.py fetch-routes input.kml output.kml \
        --osm-tags "route=train,operator=East Midlands Railway|CrossCountry" --osm-names endpoints \
        --layer-name "Train Routes" --osm-file nottinghamshire-latest.osm.pbf --side-by-side

    # Export hiding zones of radius 0.4 km and custom matching/measuring question presets for the
    # Taibeled JetLag Hide and Seek Map Generator (https://taibeled.github.io/JetLagHideAndSeek/), to load with its
    # "Load File", as taibeled.json
    python hide_and_seek_enhancer.py export-taibeled input.kml \
        --zones-layer "hiding zones" --radius 0.4 --radius-unit kilometers \
        --question-layers "1st admin,2nd admin,RIVERS,pois" --boundary-layer "game boandaries"

Dependencies:
    pip install geopandas scipy shapely pyproj fiona lxml requests contourpy pillow osmium
"""

from hns.cli import main

if __name__ == "__main__":
    main()
