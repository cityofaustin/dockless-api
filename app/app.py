"""
Dockless origin/destination trip data API
# try me
http://localhost:8000/v1/trips?xy=-97.75094341278084,30.276185988411257&flow=destination&mode=all

#TODO:
- query params for dow, hour day, date
"""
import argparse
import json
import os
import urllib.request

import requests
from rtree import index
from sanic import Sanic
from sanic import response
from sanic import exceptions
from sanic_cors import CORS, cross_origin
from shapely.geometry import Point, shape, asPolygon, mapping, polygon
from shapely.ops import cascaded_union


def spatial_index(features):
    # create spatial index of grid cell features
    # features: geojson feature array
    idx = index.Index()
    for pos, feature in enumerate(features):
        idx.insert(pos, shape(feature["geometry"]).bounds)

    return idx


def parse_flow(args):
    if not args.get("flow") or args.get("flow") == "origin":
        return "origin"
    elif args.get("flow") == "destination":
        return "destination"
    else:
        raise exceptions.ServerError("Unsupported flow specified. Must be either origin (default) or destination.", status_code=500)


def parse_mode(args):
    if not args.get("mode") or args.get("mode") == "all":
        return "total"
    elif args.get("mode") == "scooter":
        return "scooter"
    elif args.get("mode") == "bicycle":
        return "bicycle"
    else:
        raise exceptions.ServerError("Unsupported mode specified. Must be either scooter, bicycle, or all (default).", status_code=500)


def parse_coordinates(args):
    if not args.get("xy"):
        raise exceptions.ServerError("XY parameter is requried.", status_code=500)

    elements = args.get("xy").split(",")

    try:
        elements = [float(elem) for elem in elements]
    except ValueError:
        raise exceptions.ServerError("Unable to handle xy. Verify that xy is a comma-separated string of numbers.", status_code=500)

    return [tuple(elements[x : x + 2]) for x in range(0, len(elements), 2)]


def get_query_geom(coords):
    if len(coords) == 1:
        return Point(coords)
    elif len(coords) > 2:
        return asPolygon(coords)
    else:
        raise exceptions.ServerError("Insufficient xy coordinates provided. A LinearRing must have at least 3 coordinate tuples.", status_code=500)


def get_intersect_features(query_geom, grid, idx, id_property="id"):
    # get the grid cells that intersect with the request geometry
    # see: https://stackoverflow.com/questions/14697442/faster-way-of-polygon-intersection-with-shapely
    ids = []
    polys = []

    if isinstance(query_geom, polygon.PolygonAdapter):
        coords = query_geom.exterior.coords
    else:
        coords = query_geom.coords

    # reduce intersection feature set with rtree (this tests polygon bbox intersection)
    for intersect_pos in idx.intersection(query_geom.bounds):

        grid_id = list(grid.keys())[intersect_pos]
        poly = shape(grid[grid_id]["geometry"])

        # check if poly actually interesects with request geom
        if query_geom.intersects(poly):
            ids.append(grid[grid_id]["properties"][id_property])
            polys.append(poly)

    return ids, polys

def get_flow_keys(flow):
    '''
    Bit of harcoding to map the flow to the corresponding dataset property
    '''
    if flow == "origin":
        flow_key_init = "orig_cell_id"
        flow_key_end = "dest_cell_id"
    elif flow == "destination":
        flow_key_init = "dest_cell_id"
        flow_key_end = "orig_cell_id"
    else:
        # this should never happen because we validate the flow param when parsing
        # the request
        raise exceptions.ServerError("Unsupported flow specified. Must be either origin (default) or destination.", status_code=500)

    return [flow_key_init, flow_key_end]


def get_trips(intersect_ids, flow_keys, mode):
    '''
    Given a list of cell ids, extract trip count properties from the source grid data.
    '''

    # this flow O/D stuff can get confusing, so let's name these list elements
    flow_key_init = flow_keys[0]
    flow_key_end = flow_keys[1]

    # generate a string of single-quoted ids (as if for a SQL `IN ()` statement)
    intersect_id_string = ', '.join([f"'{id_}'" for id_ in intersect_ids])

    # todo: placeholder for aditional query handling
    query_params = {
        'start_time' : '',
        'end_time' : '',
        'council_district_start' : '',
        'council_district_end' : '',
        'dow' : '',
        'month' : '',
        'hour' : '',
    }

    query = f"select count(*) as trip_count, {flow_key_end} where {flow_key_init} in ({intersect_id_string}) and {flow_key_init} not in ('OUT_OF_BOUNDS') and {flow_key_end} not in ('OUT_OF_BOUNDS') group by {flow_key_end} limit 10000000"

    params = { "$query" : query }

    res = requests.get(TRIPS_URL, params, timeout=90)

    res.raise_for_status()

    return res.json()


def build_geojson(grid, trips, flow_key_start):
    '''
    Combine trip counts with their corresponding geojson feature, returning a geojson
    object with counts assigned to `trips` property
    '''
    geojson = {"type":"FeatureCollection","features":[]}

    for cell in trips:
        cell_id = cell.get(flow_key_start)
        feature = grid.get(cell_id)

        count = int(cell.get("trip_count"))

        count_as_height =  count / 5  # each 5 trips will equate to 1 meter of height on the map

        feature["properties"]["trips"] = count
        feature["properties"]["count_as_height"] = count_as_height
        feature["properties"]["cell_id"] = int(cell_id)
        feature["properties"]["trips"] = count
        geojson["features"].append(feature)

    return geojson


def get_total_trips(trips):
    return sum([int(trip["trip_count"]) for trip in trips])
    

dirname = os.path.dirname(__file__)
source = os.path.join(dirname, "data/hex500_indexed.json")

with open(source, "r") as fin:

    TRIPS_URL =  "https://data.austintexas.gov/resource/pqaf-uftu.json"
    
    grid = json.loads(fin.read())
    idx = spatial_index(grid[feature_id] for feature_id in grid.keys())
    app = Sanic(__name__)
    CORS(app)


@app.get("/trips", version=1)
async def trip_handler(request):
    flow = parse_flow(request.args)

    flow_keys = get_flow_keys(flow)

    mode = parse_mode(request.args)

    coords = parse_coordinates(request.args)

    query_geom = get_query_geom(coords)

    intersect_ids, intersect_polys = get_intersect_features(query_geom, grid, idx)

    trips = get_trips(intersect_ids, flow_keys, mode)

    response_data = {}

    response_data['features'] = build_geojson(grid, trips, flow_keys[1])

    response_data ['total_trips'] = get_total_trips(trips)

    intersect_poly = cascaded_union(intersect_polys)

    response_data["intersect_feature"] = mapping(intersect_poly)

    return response.json(response_data)

@app.route('/reload', version=1)
async def index(request):
    urllib.request.urlretrieve(os.getenv("DATABASE_URL"), "/app/data/hex500_indexed.json")
    return response.text("Reloaded")

@app.route('/', version=1)
async def index(request):
    return response.text("Hello World")

@app.exception(exceptions.NotFound)
async def ignore_404s(request, exception):
    return response.text("Page not found: {}".format(request.url))

#
# TODO: does this break the app deployment? Handy for local deve but seem to remember
# TODO: a good reason for removing it
#
# if __name__ == "__main__":
#     app.run(host="0.0.0.0", port=8000, debug=True)