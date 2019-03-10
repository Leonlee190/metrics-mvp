import json
import time
import requests

from datetime import datetime, timedelta, timezone
from geopy.distance import distance

import pandas as pd
import numpy as np

from typing import List, Union

from . import nextbus

def list_string(arr):
    return f"{[*map(str,arr)]}".replace("'",'"')

def query_graphql(route, dt_params=(2018,10,15,0,1,0), hours=20, per_hour=1):
    route_data=[]
    if type(route) != list:
        route = [route]

    per_hour=int(per_hour)
    timestamp=int(datetime(*dt_params, tzinfo = timezone(timedelta(hours = -8))).timestamp())*1000 # use PST for tzinfo
    T0 = time.time()
    for i in range(hours*per_hour):
        t0=time.time()
        # possible rounding issue here - times near beginning/end of a day can return data from the previous/next day
        start_time = timestamp + i*36e5/per_hour
        end_time   = start_time + 36e5/per_hour

        # added direction id to query, needed to obtain stops
        query = f"""{{
          trynState(agency: "muni", startTime: "{start_time}", endTime: "{end_time}", routes: {list_string(route)}) {{
            agency
            startTime
            routes {{
              rid
              stops {{
                sid
                name
                lat
                lon
              }}
              routeStates {{
                vtime
                vehicles {{
                  vid
                  lat
                  lon
                  heading
                  did
                }}
              }}
            }}
          }}
        }}
        """ # Braces need to be doubled for f-string

        query_url = "https://06o8rkohub.execute-api.us-west-2.amazonaws.com/dev/graphql?query="+query
        r = requests.get(query_url)

        data = json.loads(r.text)

        try:
            data['data']
        except KeyError as err:
            print(f"Error for time range {startTime}-{endTime}: {err}")
        else:
            if len(data['data']['trynState']['routes']):
                route_data.extend(data['data']['trynState']['routes'])

    r_sort = {}
    for x in route_data:
        if x['rid'] not in r_sort.keys():
            r_sort[x['rid']] = {'rid':x['rid'],'routeStates':[],'stops':x['stops']}
        r_sort[x['rid']]['routeStates'].extend(sorted(x['routeStates'], key=lambda rs: int(rs['vtime'])))
    r_sort=list(r_sort.values())
    return r_sort


def produce_stops(data: list, route: str) -> pd.DataFrame:
    stops = pd.io.json.json_normalize(data,
                                      record_path=['stops']) \
            .rename(columns={'lat': 'LAT',
                             'lon': 'LON',
                             'sid': 'SID'}) \
            .reindex(['SID', 'LAT', 'LON'], axis='columns')

    route_config = nextbus.get_route_config('sf-muni', route)

    # obtain stop directions
    stops['DID'] = stops['SID'].map({stop['tag']: direction['tag']
                                     for direction in route_config['route']['direction']
                                     for stop in direction['stop']})

    # remove stops that don't have an associated direction
    stops = stops.dropna(axis='index', subset=['DID'])

    # obtain stop ordinals
    stops['ORD'] = stops['SID'].map({stop_meta['tag']: ordinal
                                     for ordinal, stop_meta
                                     in enumerate(route_config['route']['stop'])})

    return stops

def produce_buses(data: list) -> pd.DataFrame:
     return pd.io.json.json_normalize(data,
                                      record_path=['routeStates', 'vehicles'],
                                      meta=[['routeStates', 'vtime']]) \
            .rename(columns={'lat': 'LAT',
                             'lon': 'LON',
                             'vid': 'VID',
                             'did': 'DID',
                             'routeStates.vtime': 'TIME'}) \
            .reindex(['TIME', 'VID', 'LAT', 'LON', 'DID'], axis='columns')

# haversine formula for calcuating distance between two coordinates in lat lon
# from bird eye view; seems to be +- 8 meters difference from geopy distance
def haver_distance(latstop,lonstop,latbus,lonbus):

    latstop,lonstop,latbus,lonbus = map(np.deg2rad,[latstop,lonstop,latbus,lonbus])
    eradius = 6371000

    latdiff = (latbus-latstop)
    londiff = (lonbus-lonstop)

    a = np.sin(latdiff/2)**2 + np.cos(latstop)*np.cos(latbus)*np.sin(londiff/2)**2
    c = 2*np.arctan2(np.sqrt(a),np.sqrt(1-a))

    distance = eradius*c
    return distance

def find_eclipses(buses, stop):
    """
    Find movement of buses relative to the stop, in distance as a function of time.
    """
    def split_eclipses(eclipses, threshold=30*60*1000) -> List[pd.DataFrame]:
        """
        Split buses' movements when they return to a stop after completing the route.
        """
        disjoint_eclipses = []
        for bus_id in eclipses['VID'].unique(): # list of unique VID's
            # obtain distance data for this one bus
            bus = eclipses[eclipses['VID'] == bus_id].sort_values('TIME')
            #pprint.pprint(bus)
            #pprint.pprint(bus['TIME'].shift())
            #pprint.pprint(bus['TIME'].shift() + threshold)
            #print('===============')
            # split data into groups when there is at least a `threshold`-ms gap between data points
            group_ids = (bus['TIME'] > (bus['TIME'].shift() + threshold)).cumsum()

            # store groups
            for _, group in bus.groupby(group_ids):
                disjoint_eclipses.append(group)
        return disjoint_eclipses

    eclipses = buses.copy()
    #eclipses['DIST'] = eclipses.apply(lambda row: distance(stop[['LAT','LON']],row[['LAT','LON']]).meters,axis=1)

    stopcord = stop[['LAT', 'LON']]
    buscord = eclipses[['LAT', 'LON']]

    # calculate distances fast with haversine function
    eclipses['DIST'] = haver_distance(stopcord['LAT'],stopcord['LON'],buscord['LAT'],buscord['LON'])
    # only keep positions within 750 meters within the given stop; (filtering out)
    eclipses = eclipses[eclipses['DIST'] < 750]

    # update the coordinates list
    stopcord = stop[['LAT', 'LON']].values
    buscord = eclipses[['LAT', 'LON']].values

    # calculate distances again using geopy for the distance<750m values, because geopy is probably more accurate
    # dfromstop = []
    # for row in buscord:
    #     busdistance = distance(stopcord,row).meters
    #     dfromstop.append(busdistance)
    # eclipses['DIST'] = dfromstop

    # for haversine function:
    stopcord = stop[['LAT', 'LON']]
    buscord = eclipses[['LAT', 'LON']]
    eclipses['DIST'] = haver_distance(stopcord['LAT'],stopcord['LON'],buscord['LAT'],buscord['LON'])

    eclipses['TIME'] = eclipses['TIME'].astype(np.int64)
    eclipses = eclipses[['TIME', 'VID', 'DIST']]

    eclipses = split_eclipses(eclipses)

    return eclipses

def find_nadirs(eclipses):
    """
    Find points where buses are considered to have encountered the stop.

    Nadir is an astronomical term that describes the lowest point reached by an orbiting body.
    """
    def calc_nadir(eclipse: pd.DataFrame) -> Union[pd.Series, None]:
        nadir = eclipse.iloc[eclipse['DIST'].values.argmin()]
        if nadir['DIST'] < 100:  # if min dist < 100, then reasonable candidate for nadir
            return nadir
        else:  # otherwise, hardcore datasci is needed
            rev_eclipse = eclipse.iloc[::-1]
            rev_nadir = rev_eclipse.iloc[rev_eclipse['DIST'].values.argmin()]
            if nadir['TIME'] == rev_nadir['TIME']:  # if eclipse has a global min
                return nadir  # then it's the best candidate for nadir
            else:  # if eclipse's min occurs at two times
                mid_nadir = nadir.copy()
                mid_nadir['DIST'] = (nadir['DIST'] + rev_nadir['DIST'])/2
                return mid_nadir  # take the midpoint of earliest and latest mins

    nadirs = []
    for eclipse in eclipses:
        nadirs.append(calc_nadir(eclipse)[['VID', 'TIME']])

    return pd.DataFrame(nadirs)