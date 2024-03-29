#!/usr/bin/env python

# Copyright (C) 2017 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


from __future__ import print_function
from datetime import date, timedelta
import argparse
import json
import os.path
import pathlib2 as pathlib
import re
import subprocess
import time

import google.oauth2.credentials

from google.assistant.library import Assistant
from google.assistant.library.event import EventType
from google.assistant.library.file_helpers import existing_file
from google.assistant.library.device_helpers import register_device

import snowboywave
import synthesize_text

import faasshell

try:
    from BMP180 import BMP180
    import dht11
    from gpiozero import LED
    import lightsensor
    GPIO_FLAG = True
except:
    GPIO_FLAG = False

import faulthandler
faulthandler.enable()

try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError


WARNING_NOT_REGISTERED = """
    This device is not registered. This means you will not be able to use
    Device Actions or see your device in Assistant Settings. In order to
    register this device follow instructions at:

    https://developers.google.com/assistant/sdk/guides/library/python/embed/register-device
"""

if GPIO_FLAG:
    LED23 = LED(23)
    bmp = BMP180()
    lightsensor.setup()


def process_event(event, assistant):
    """Pretty prints events.

    Prints all events that occur with two spaces between each new
    conversation and a single space between turns of a conversation.

    Args:
        event(event.Event): The current event to process.
    """
    if event.type == EventType.ON_CONVERSATION_TURN_STARTED:
        snowboywave.play_audio_file(snowboywave.DETECT_DING)
        if GPIO_FLAG:
            LED23.on()
        print()

    print(event)

    if (event.type == EventType.ON_CONVERSATION_TURN_FINISHED and
            event.args and not event.args['with_follow_on_turn']):
        snowboywave.play_audio_file(snowboywave.DETECT_DONG)
        if GPIO_FLAG:
            LED23.off()
        print()
    if event.type == EventType.ON_DEVICE_ACTION:
        assistant.stop_conversation()
        for command, params in event.actions:
            print('Do command', command, 'with params', str(params))
            if command == "action.devices.commands.OnOff":
                try:
                    if params['on']:
                        subprocess.check_call('./bin/tentou')
                        print('Turning the ligtht on.')
                    else:
                        subprocess.check_call('./bin/shoutou')
                        print('Turning the light off.')
                except:
                    print("subprocess.check_call() failed")

            if command == "io.github.naohirotamura.commands.ReportLightSensor":
                if GPIO_FLAG:
                    ratio = lightsensor.read_lightsensor_adc_ratio()
                else:
                    ratio = 0
                print('Reporting light sensor AD converter ratio: %.2f percent'
                      % ratio)
                synthesize_text.synthesize_text(
                    'ライトセンサー AD コンバーター比は %.2f パーセントです' % ratio)

            if command == "io.github.naohirotamura.commands.ReportHumidity":
                for i in range(20):
                    if GPIO_FLAG:
                        result = dht11.read_dht11_dat()
                    else:
                        result = [0,0]
                    if result:
                        break
                    else:
                        print("%s: Data not good, skip" % i)
                        time.sleep(0.5)
                if result:
                    humidity, temperature = result
                    print("Reporting humidity: %s %%,  Temperature: %s C"
                          % (humidity, temperature))
                    synthesize_text.synthesize_text(
                        '湿度は %s パーセントです' % humidity)
                else:
                    print('Reporting humidity: timeout')
                    synthesize_text.synthesize_text(
                        '湿度の取得はタイムアウトしました')

            if command == "io.github.naohirotamura.commands.ReportAltitude":
                if GPIO_FLAG:
                    altitude = bmp.read_altitude()
                else:
                    altitude = 0
                print('Reporting altitude: %.2f meter' % altitude)
                synthesize_text.synthesize_text(
                    '標高は %.2f メートルです' % altitude)

            if command == "io.github.naohirotamura.commands.ReportTemperature":
                if GPIO_FLAG:
                    temperature = bmp.read_temperature()
                else:
                    temperature = 0
                print('Reporting room temperature: %.2f C' % temperature)
                synthesize_text.synthesize_text(
                    '部屋の気温は %.2f 度です' % temperature)

            if command == "io.github.naohirotamura.commands.ReportPressure":
                if GPIO_FLAG:
                    pressure = bmp.read_pressure() / 100.0
                else:
                    pressure = 0
                print('Reporting pressure: %.2f hPa' % pressure)
                synthesize_text.synthesize_text(
                    '部屋の気圧は %.2f ヘクトパスカルです' % pressure)

            if command == "com.fujitsu.commands.CommitCountReport":
                OWNER = {'faasshell': 'naohirotamura',
                         'buildah': 'containers',
                         'kubernetes': 'kubernetes'}

                date_pattern = re.compile('(\d{4})年(\d{1,2})月(\d{1,2})日')

                print('Querying', params['repository'], 'from', params['start'], 'to', params['end'])
                if params['start'] == '':
                    start = date.today().strftime("%Y-%m-%d") + "T00:00:00+00:00"
                else:
                    y1,m1,d1 = date_pattern.search(params['start']).groups()
                    start = date(int(y1),int(m1),int(d1)).strftime("%Y-%m-%d") + "T00:00:00+00:00"
                if params['end'] == '':
                    end = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d") + "T00:00:00+00:00"
                else:
                    y2,m2,d2 = date_pattern.search(params['end']).groups()
                    end = date(int(y2),int(m2),int(d2)).strftime("%Y-%m-%d") + "T00:00:00+00:00"

                print('owner:', OWNER[params['repository']], 'start:', start, 'end:', end)
                result = faasshell.commit_count_report(OWNER[params['repository']], params['repository'],
                                                       start, end)
                print('result:', result)
                if 'error' in result.keys():
                    print('Commit count report returned error', result['error'])
                    synthesize_text.synthesize_text(
                        'コミットカウントレポートはエラーになりました')
                else:
                    report = result['output']['github']['output']['values'][0]
                    print('Commit count report returned ', report)
                    synthesize_text.synthesize_text(
                        'コミットカウントレポートによると、リポジトリ'
                        + report[2] + ' へ' + report[0] + 'は、コミット数'
                        + str(report[5]) + ' 件の貢献をしました')


def main():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('--device-model-id', '--device_model_id', type=str,
                        metavar='DEVICE_MODEL_ID', required=False,
                        help='the device model ID registered with Google')
    parser.add_argument('--project-id', '--project_id', type=str,
                        metavar='PROJECT_ID', required=False,
                        help='the project ID used to register this device')
    parser.add_argument('--nickname', type=str,
                        metavar='NICKNAME', required=False,
                        help='the nickname used to register this device')
    parser.add_argument('--device-config', type=str,
                        metavar='DEVICE_CONFIG_FILE',
                        default=os.path.join(
                            os.path.expanduser('~/.config'),
                            'googlesamples-assistant',
                            'device_config_library.json'
                        ),
                        help='path to store and read device configuration')
    parser.add_argument('--credentials', type=existing_file,
                        metavar='OAUTH2_CREDENTIALS_FILE',
                        default=os.path.join(
                            os.path.expanduser('~/.config'),
                            'google-oauthlib-tool',
                            'credentials.json'
                        ),
                        help='path to store and read OAuth2 credentials')
    parser.add_argument('--query', type=str,
                        metavar='QUERY',
                        help='query to send as soon as the Assistant starts')
    parser.add_argument('-v', '--version', action='version',
                        version='%(prog)s ' + Assistant.__version_str__())

    args = parser.parse_args()
    with open(args.credentials, 'r') as f:
        credentials = google.oauth2.credentials.Credentials(token=None,
                                                            **json.load(f))

    device_model_id = None
    last_device_id = None
    try:
        with open(args.device_config) as f:
            device_config = json.load(f)
            device_model_id = device_config['model_id']
            last_device_id = device_config.get('last_device_id', None)
    except FileNotFoundError:
        pass

    if not args.device_model_id and not device_model_id:
        raise Exception('Missing --device-model-id option')

    # Re-register if "device_model_id" is given by the user and it differs
    # from what we previously registered with.
    should_register = (
        args.device_model_id and args.device_model_id != device_model_id)

    device_model_id = args.device_model_id or device_model_id

    with Assistant(credentials, device_model_id) as assistant:
        events = assistant.start()

        device_id = assistant.device_id
        print('device_model_id:', device_model_id)
        print('device_id:', device_id + '\n')

        # Re-register if "device_id" is different from the last "device_id":
        if should_register or (device_id != last_device_id):
            if args.project_id:
                register_device(args.project_id, credentials,
                                device_model_id, device_id, args.nickname)
                pathlib.Path(os.path.dirname(args.device_config)).mkdir(
                    exist_ok=True)
                with open(args.device_config, 'w') as f:
                    json.dump({
                        'last_device_id': device_id,
                        'model_id': device_model_id,
                    }, f)
            else:
                print(WARNING_NOT_REGISTERED)

        for event in events:
            if event.type == EventType.ON_START_FINISHED and args.query:
                assistant.send_text_query(args.query)

            process_event(event, assistant)


if __name__ == '__main__':
    main()
