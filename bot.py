import json
import os
import pytz
import requests
from apscheduler.schedulers.blocking import BlockingScheduler
from datetime import datetime
from dotenv import load_dotenv
from geopy import distance
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from timezonefinder import TimezoneFinder

# Globals
configs = []
message = ''
timezoneFinder = TimezoneFinder()

hyvee_locations = []
hyvee_availability = {}
newly_available_hyvee_appointments = []

spotter_cache = {}
spotter_locations = []
spotter_availability = {}
newly_available_spotter_appointments = []

# Load config
files = [file for file in os.listdir('config/') if file.endswith('.json')]
for file in files:
    with open(f'config/{file}') as f:
        configs.append(json.load(f))
        print(f'config file loaded: {file}')


# Hy-Vee Pharmacies
def get_hyvee_vaccine_availability(latitude, longitude, radius):
    global hyvee_locations
    url = 'https://www.hy-vee.com/my-pharmacy/api/graphql'
    query = '''
        query SearchPharmaciesNearPointWithCovidVaccineAvailability($latitude: Float!, $longitude: Float!, $radius: Int! = 10) {
            searchPharmaciesNearPoint(latitude: $latitude, longitude: $longitude, radius: $radius) {
                distance
                location {
                    locationId
                    name
                    nickname
                    phoneNumber
                    businessCode
                    isCovidVaccineAvailable
                    covidVaccineEligibilityTerms
                    address {
                        line1
                        line2
                        city
                        state
                        zip
                        latitude
                        longitude
                        __typename
                    }
                    __typename
                }
                __typename
            }
        }
    '''
    response = requests.post(url, json={
        'query': query,
        'variables': {
            'latitude': latitude,
            'longitude': longitude,
            'radius': radius,
        }
    })

    if response.status_code == 200:
        json_data = json.loads(response.text)
        hyvee_locations = list(map(lambda location: location['location'], json_data['data']['searchPharmaciesNearPoint']))

    else:
        print(f'Error getting vaccine availability: {response.text}')
        hyvee_locations = []


def get_newly_available_hyvee_locations(is_test=False):
    global hyvee_locations, hyvee_availability, newly_available_hyvee_appointments
    newly_available_hyvee_appointments = []

    for location in hyvee_locations:
        prev_avail = hyvee_availability[location['locationId']] if location['locationId'] in hyvee_availability else False

        if location['isCovidVaccineAvailable'] and not prev_avail:
            newly_available_hyvee_appointments.append(location)

        if not len(newly_available_hyvee_appointments) and is_test and not prev_avail:
            newly_available_hyvee_appointments.append(location)

        hyvee_availability[location['locationId']] = location['isCovidVaccineAvailable']


# Other Pharmacies - vaccinespotter.org API
def get_spotter_api_vaccine_availability(latitude, longitude, radius, states):
    global spotter_cache, spotter_locations
    locations = []
    for state in states:
        if state not in spotter_cache:
            response = requests.get(f'https://www.vaccinespotter.org/api/v0/states/{state}.json')

            if response.status_code == 200:
                json_data = json.loads(response.text)
                spotter_cache[state] = list(
                    filter(lambda loc: loc['properties']['provider'] != 'hyvee', json_data['features'])
                )
            else:
                spotter_cache[state] = []

        locations.extend(spotter_cache[state])

    spotter_locations = []
    search_coordinates = (latitude, longitude)
    for location in locations:
        location_coordinates = (location['geometry']['coordinates'][1], location['geometry']['coordinates'][0])
        if distance.distance(search_coordinates, location_coordinates).miles <= radius:
            spotter_locations.append(location)


def get_newly_available_spotter_locations(is_test=False):
    global spotter_locations, spotter_availability, newly_available_spotter_appointments
    newly_available_spotter_appointments = []

    for location in spotter_locations:
        prev_avail = spotter_availability[location['properties']['id']] if location['properties']['id'] in spotter_availability else False

        if location['properties']['appointments_available'] and not prev_avail:
            newly_available_spotter_appointments.append(location)

        if not len(newly_available_spotter_appointments) and is_test and not prev_avail:
            newly_available_spotter_appointments.append(location)

        spotter_availability[location['properties']['id']] = location['properties']['appointments_available']


# Message blocks
def header_block():
    global message
    message = ':alert: <!here> *Vaccines Available* :alert:'
    return {
        'type': 'section',
        'text': {
            'type': 'mrkdwn',
            'text': ':alert: <!here> *Vaccines Available* :alert:\n\nThe following locations have COVID-19 vaccination appointments :covid-19: :syringe: available now!'
        }
    }


def location_hyvee_block(location):
    global message
    name = location['nickname'] if location['nickname'] else location['name']
    address = location['address']['line1']
    city = location['address']['city']
    state = location['address']['state']
    zipcode = location['address']['zip']
    message += f'\n\n*{name}*\n{address}\n{city}, {state} {zipcode}'
    return {
        'type': 'section',
        'text': {
            'type': 'mrkdwn',
            'text': f'*{name}*\n{address}\n{city}, {state} {zipcode}'
        }
    }


def location_spotter_block(location):
    global message

    if location['properties']['provider'] == 'cvs':
        city = location['properties']['city']
        state = location['properties']['state']
        text = f'*CVS*\n{city}, {state}'

    else:
        name = '{name} {provider}'.format(
            name=location['properties']['name'],
            provider=location['properties']['provider_brand_name']
        )
        address = location['properties']['address']
        city = location['properties']['city']
        state = location['properties']['state']
        zipcode = location['properties']['postal_code']
        text = f'*{name}*\n{address}\n{city}, {state} {zipcode}'

    message += f'\n\n{text}'
    return {
        'type': 'section',
        'text': {
            'type': 'mrkdwn',
            'text': text
        }
    }


def url_block(url):
    global message
    message += f'\n{url}'
    return {
        'type': 'actions',
        'elements': [
            {
                'type': 'button',
                'text': {
                    'type': 'plain_text',
                    'emoji': True,
                    'text': 'Register Now'
                },
                'style': 'primary',
                'url': url
            }
        ]
    }


def divider_block():
    return {
        'type': 'divider'
    }


def footer_block(latitude, longitude):
    global message, timezoneFinder
    timezone = timezoneFinder.timezone_at(lng=longitude, lat=latitude)
    tz = pytz.timezone(timezone)
    dt = datetime.now(tz)
    timestamp = dt.strftime('%b %d, %Y at %I:%M:%S %p %Z')
    message += f'\n\n_Posted {timestamp}_'
    return {
        'type': 'section',
        'text': {
            'type': 'mrkdwn',
            'text': f'_Posted {timestamp}_'
        }
    }


# Main
def check_for_vaccine_availability():
    global configs, message, spotter_cache, newly_available_hyvee_appointments, newly_available_spotter_appointments

    # clear the spotter cache
    spotter_cache = {}

    for config in configs:
        enabled = config['enabled'] if 'enabled' in config else True

        if enabled:
            if config['latitude'] and config['longitude'] and config['radius']:
                is_test = config['test'] if 'test' in config else False
                get_hyvee_vaccine_availability(config['latitude'], config['longitude'], config['radius'])
                get_newly_available_hyvee_locations(is_test)

                # clear the spotter cache
                states = config['states'] if 'states' in config else ['NE']
                get_spotter_api_vaccine_availability(
                    config['latitude'],
                    config['longitude'],
                    config['radius'],
                    states)
                get_newly_available_spotter_locations(is_test)
            else:
                print('invalid config: missing geolocation data')

            if len(newly_available_hyvee_appointments) or len(newly_available_spotter_appointments):
                print(f'{len(newly_available_hyvee_appointments)} newly available hy-vee location(s) found')
                print(f'{len(newly_available_spotter_appointments)} newly available spotter location(s) found')

                blocks = [header_block()]
                for location in newly_available_hyvee_appointments:
                    blocks.append(location_hyvee_block(location))
                    blocks.append(url_block('https://www.hy-vee.com/my-pharmacy/covid-vaccine-consent'))
                    blocks.append(divider_block())

                for location in newly_available_spotter_appointments:
                    blocks.append(location_spotter_block(location))
                    blocks.append(url_block(location['properties']['url']))
                    blocks.append(divider_block())

                blocks.append(footer_block(config['latitude'], config['longitude']))

                # send the Slack message
                if config['channel']:
                    try:
                        client = WebClient(token=os.environ['SLACK_BOT_TOKEN'])
                        client.chat_postMessage(channel=config['channel'], blocks=blocks, text=message)
                    except SlackApiError as e:
                        print('Error sending Slack message: {message}'.format(message=e.response['error']))

                else:
                    print('invalid config: missing Slack channel')


# Run the schedule
load_dotenv()
scheduler = BlockingScheduler()
scheduler.add_job(check_for_vaccine_availability, 'interval', seconds=30)
scheduler.start()
