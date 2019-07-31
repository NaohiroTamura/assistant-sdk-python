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

"""Sample that implements a gRPC client for the Google Assistant API."""

from datetime import date, timedelta
import concurrent.futures
import json
import logging
import os
import os.path
import pathlib2 as pathlib
import re
import sys
import subprocess
import time
import uuid

import click
import grpc
import google.auth.transport.grpc
import google.auth.transport.requests
import google.oauth2.credentials

from google.assistant.embedded.v1alpha2 import (
    embedded_assistant_pb2,
    embedded_assistant_pb2_grpc
)
from tenacity import retry, stop_after_attempt, retry_if_exception

try:
    from . import (
        assistant_helpers,
        audio_helpers,
        browser_helpers,
        device_helpers,
        snowboydecoder
    )
except (SystemError, ImportError):
    import assistant_helpers
    import audio_helpers
    import browser_helpers
    import device_helpers
    import snowboydecoder

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


ASSISTANT_API_ENDPOINT = 'embeddedassistant.googleapis.com'
END_OF_UTTERANCE = embedded_assistant_pb2.AssistResponse.END_OF_UTTERANCE
DIALOG_FOLLOW_ON = embedded_assistant_pb2.DialogStateOut.DIALOG_FOLLOW_ON
CLOSE_MICROPHONE = embedded_assistant_pb2.DialogStateOut.CLOSE_MICROPHONE
PLAYING = embedded_assistant_pb2.ScreenOutConfig.PLAYING
DEFAULT_GRPC_DEADLINE = 60 * 3 + 5

logger = logging.getLogger(__name__)

if GPIO_FLAG:
    LED23 = LED(23)
    bmp = BMP180()
    lightsensor.setup()


class SampleAssistant(object):
    """Sample Assistant that supports conversations and device actions.

    Args:
      device_model_id: identifier of the device model.
      device_id: identifier of the registered device instance.
      conversation_stream(ConversationStream): audio stream
        for recording query and playing back assistant answer.
      channel: authorized gRPC channel for connection to the
        Google Assistant API.
      deadline_sec: gRPC deadline in seconds for Google Assistant API call.
      device_handler: callback for device actions.
    """

    def __init__(self, language_code, device_model_id, device_id,
                 conversation_stream, display,
                 channel, deadline_sec, device_handler):
        self.language_code = language_code
        self.device_model_id = device_model_id
        self.device_id = device_id
        self.conversation_stream = conversation_stream
        self.display = display

        # Opaque blob provided in AssistResponse that,
        # when provided in a follow-up AssistRequest,
        # gives the Assistant a context marker within the current state
        # of the multi-Assist()-RPC "conversation".
        # This value, along with MicrophoneMode, supports a more natural
        # "conversation" with the Assistant.
        self.conversation_state = None
        # Force reset of first conversation.
        self.is_new_conversation = True

        # Create Google Assistant API gRPC client.
        self.assistant = embedded_assistant_pb2_grpc.EmbeddedAssistantStub(
            channel
        )
        self.deadline = deadline_sec

        self.device_handler = device_handler

    def __enter__(self):
        return self

    def __exit__(self, etype, e, traceback):
        if e:
            return False
        self.conversation_stream.close()

    def is_grpc_error_unavailable(e):
        is_grpc_error = isinstance(e, grpc.RpcError)
        if is_grpc_error and (e.code() == grpc.StatusCode.UNAVAILABLE):
            logger.error('grpc unavailable error: %s', e)
            return True
        return False

    @retry(reraise=True, stop=stop_after_attempt(3),
           retry=retry_if_exception(is_grpc_error_unavailable))
    def assist(self):
        """Send a voice request to the Assistant and playback the response.

        Returns: True if conversation should continue.
        """
        continue_conversation = False
        device_actions_futures = []

        self.conversation_stream.start_recording()
        logger.info('Recording audio request.')

        def iter_log_assist_requests():
            for c in self.gen_assist_requests():
                assistant_helpers.log_assist_request_without_audio(c)
                yield c
            logger.debug('Reached end of AssistRequest iteration.')

        # This generator yields AssistResponse proto messages
        # received from the gRPC Google Assistant API.
        for resp in self.assistant.Assist(iter_log_assist_requests(),
                                          self.deadline):
            assistant_helpers.log_assist_response_without_audio(resp)
            if resp.event_type == END_OF_UTTERANCE:
                logger.info('End of audio request detected.')
                logger.info('Stopping recording.')
                self.conversation_stream.stop_recording()
            if resp.speech_results:
                logger.info('Transcript of user request: "%s".',
                             ' '.join(r.transcript
                                      for r in resp.speech_results))
            if len(resp.audio_out.audio_data) > 0:
                if not self.conversation_stream.playing:
                    self.conversation_stream.stop_recording()
                    self.conversation_stream.start_playback()
                    logger.info('Playing assistant response.')
                self.conversation_stream.write(resp.audio_out.audio_data)
            if resp.dialog_state_out.conversation_state:
                conversation_state = resp.dialog_state_out.conversation_state
                logger.debug('Updating conversation state.')
                self.conversation_state = conversation_state
            if resp.dialog_state_out.volume_percentage != 0:
                volume_percentage = resp.dialog_state_out.volume_percentage
                logger.info('Setting volume to %s%%', volume_percentage)
                self.conversation_stream.volume_percentage = volume_percentage
            if resp.dialog_state_out.microphone_mode == DIALOG_FOLLOW_ON:
                continue_conversation = True
                logger.info('Expecting follow-on query from user.')
            elif resp.dialog_state_out.microphone_mode == CLOSE_MICROPHONE:
                continue_conversation = False
            if resp.device_action.device_request_json:
                device_request = json.loads(
                    resp.device_action.device_request_json
                )
                fs = self.device_handler(device_request)
                if fs:
                    device_actions_futures.extend(fs)
            if self.display and resp.screen_out.data:
                system_browser = browser_helpers.system_browser
                system_browser.display(resp.screen_out.data)

        if len(device_actions_futures):
            logger.info('Waiting for device executions to complete.')
            concurrent.futures.wait(device_actions_futures)

        logger.info('Finished playing assistant response.')
        self.conversation_stream.stop_playback()
        return continue_conversation

    def gen_assist_requests(self):
        """Yields: AssistRequest messages to send to the API."""

        config = embedded_assistant_pb2.AssistConfig(
            audio_in_config=embedded_assistant_pb2.AudioInConfig(
                encoding='LINEAR16',
                sample_rate_hertz=self.conversation_stream.sample_rate,
            ),
            audio_out_config=embedded_assistant_pb2.AudioOutConfig(
                encoding='LINEAR16',
                sample_rate_hertz=self.conversation_stream.sample_rate,
                volume_percentage=self.conversation_stream.volume_percentage,
            ),
            dialog_state_in=embedded_assistant_pb2.DialogStateIn(
                language_code=self.language_code,
                conversation_state=self.conversation_state,
                is_new_conversation=self.is_new_conversation,
            ),
            device_config=embedded_assistant_pb2.DeviceConfig(
                device_id=self.device_id,
                device_model_id=self.device_model_id,
            )
        )
        if self.display:
            config.screen_out_config.screen_mode = PLAYING
        # Continue current conversation with later requests.
        self.is_new_conversation = False
        # The first AssistRequest must contain the AssistConfig
        # and no audio data.
        yield embedded_assistant_pb2.AssistRequest(config=config)
        for data in self.conversation_stream:
            # Subsequent requests need audio data, but not config.
            yield embedded_assistant_pb2.AssistRequest(audio_in=data)


@click.command()
@click.option('--api-endpoint', default=ASSISTANT_API_ENDPOINT,
              metavar='<api endpoint>', show_default=True,
              help='Address of Google Assistant API service.')
@click.option('--credentials',
              metavar='<credentials>', show_default=True,
              default=os.path.join(click.get_app_dir('google-oauthlib-tool'),
                                   'credentials.json'),
              help='Path to read OAuth2 credentials.')
@click.option('--project-id',
              metavar='<project id>',
              help=('Google Developer Project ID used for registration '
                    'if --device-id is not specified'))
@click.option('--device-model-id',
              metavar='<device model id>',
              help=(('Unique device model identifier, '
                     'if not specifed, it is read from --device-config')))
@click.option('--device-id',
              metavar='<device id>',
              help=(('Unique registered device instance identifier, '
                     'if not specified, it is read from --device-config, '
                     'if no device_config found: a new device is registered '
                     'using a unique id and a new device config is saved')))
@click.option('--device-config', show_default=True,
              metavar='<device config>',
              default=os.path.join(
                  click.get_app_dir('googlesamples-assistant'),
                  'device_config.json'),
              help='Path to save and restore the device configuration')
@click.option('--lang', show_default=True,
              metavar='<language code>',
              default='en-US',
              help='Language code of the Assistant')
@click.option('--display', is_flag=True, default=False,
              help='Enable visual display of Assistant responses in HTML.')
@click.option('--verbose', '-v', is_flag=True, default=False,
              help='Verbose logging.')
@click.option('--input-audio-file', '-i',
              metavar='<input file>',
              help='Path to input audio file. '
              'If missing, uses audio capture')
@click.option('--output-audio-file', '-o',
              metavar='<output file>',
              help='Path to output audio file. '
              'If missing, uses audio playback')
@click.option('--audio-sample-rate',
              default=audio_helpers.DEFAULT_AUDIO_SAMPLE_RATE,
              metavar='<audio sample rate>', show_default=True,
              help='Audio sample rate in hertz.')
@click.option('--audio-sample-width',
              default=audio_helpers.DEFAULT_AUDIO_SAMPLE_WIDTH,
              metavar='<audio sample width>', show_default=True,
              help='Audio sample width in bytes.')
@click.option('--audio-iter-size',
              default=audio_helpers.DEFAULT_AUDIO_ITER_SIZE,
              metavar='<audio iter size>', show_default=True,
              help='Size of each read during audio stream iteration in bytes.')
@click.option('--audio-block-size',
              default=audio_helpers.DEFAULT_AUDIO_DEVICE_BLOCK_SIZE,
              metavar='<audio block size>', show_default=True,
              help=('Block size in bytes for each audio device '
                    'read and write operation.'))
@click.option('--audio-flush-size',
              default=audio_helpers.DEFAULT_AUDIO_DEVICE_FLUSH_SIZE,
              metavar='<audio flush size>', show_default=True,
              help=('Size of silence data in bytes written '
                    'during flush operation'))
@click.option('--grpc-deadline', default=DEFAULT_GRPC_DEADLINE,
              metavar='<grpc deadline>', show_default=True,
              help='gRPC deadline in seconds')
@click.option('--once', default=False, is_flag=True,
              help='Force termination after a single conversation.')
def main(api_endpoint, credentials, project_id,
         device_model_id, device_id, device_config,
         lang, display, verbose,
         input_audio_file, output_audio_file,
         audio_sample_rate, audio_sample_width,
         audio_iter_size, audio_block_size, audio_flush_size,
         grpc_deadline, once, *args, **kwargs):
    """Samples for the Google Assistant API.

    Examples:
      Run the sample with microphone input and speaker output:

        $ python -m googlesamples.assistant

      Run the sample with file input and speaker output:

        $ python -m googlesamples.assistant -i <input file>

      Run the sample with file input and output:

        $ python -m googlesamples.assistant -i <input file> -o <output file>
    """
    # Setup logging.
    # logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)

    # Load OAuth 2.0 credentials.
    try:
        with open(credentials, 'r') as f:
            credentials = google.oauth2.credentials.Credentials(token=None,
                                                                **json.load(f))
            http_request = google.auth.transport.requests.Request()
            credentials.refresh(http_request)
    except Exception as e:
        logger.error('Error loading credentials: %s', e)
        logger.error('Run google-oauthlib-tool to initialize '
                      'new OAuth 2.0 credentials.')
        sys.exit(-1)

    # Create an authorized gRPC channel.
    grpc_channel = google.auth.transport.grpc.secure_authorized_channel(
        credentials, http_request, api_endpoint)
    logger.info('Connecting to %s', api_endpoint)

    # Configure audio source and sink.
    audio_device = None
    if input_audio_file:
        audio_source = audio_helpers.WaveSource(
            open(input_audio_file, 'rb'),
            sample_rate=audio_sample_rate,
            sample_width=audio_sample_width
        )
    else:
        audio_source = audio_device = (
            audio_device or audio_helpers.SoundDeviceStream(
                sample_rate=audio_sample_rate,
                sample_width=audio_sample_width,
                block_size=audio_block_size,
                flush_size=audio_flush_size
            )
        )
    if output_audio_file:
        audio_sink = audio_helpers.WaveSink(
            open(output_audio_file, 'wb'),
            sample_rate=audio_sample_rate,
            sample_width=audio_sample_width
        )
    else:
        audio_sink = audio_device = (
            audio_device or audio_helpers.SoundDeviceStream(
                sample_rate=audio_sample_rate,
                sample_width=audio_sample_width,
                block_size=audio_block_size,
                flush_size=audio_flush_size
            )
        )
    # Create conversation stream with the given audio source and sink.
    conversation_stream = audio_helpers.ConversationStream(
        source=audio_source,
        sink=audio_sink,
        iter_size=audio_iter_size,
        sample_width=audio_sample_width,
    )

    if not device_id or not device_model_id:
        try:
            with open(device_config) as f:
                device = json.load(f)
                device_id = device['id']
                device_model_id = device['model_id']
                logger.info("Using device model %s and device id %s",
                             device_model_id,
                             device_id)
        except Exception as e:
            logger.warning('Device config not found: %s' % e)
            logger.info('Registering device')
            if not device_model_id:
                logger.error('Option --device-model-id required '
                              'when registering a device instance.')
                sys.exit(-1)
            if not project_id:
                logger.error('Option --project-id required '
                              'when registering a device instance.')
                sys.exit(-1)
            device_base_url = (
                'https://%s/v1alpha2/projects/%s/devices' % (api_endpoint,
                                                             project_id)
            )
            device_id = str(uuid.uuid1())
            payload = {
                'id': device_id,
                'model_id': device_model_id,
                'client_type': 'SDK_SERVICE'
            }
            session = google.auth.transport.requests.AuthorizedSession(
                credentials
            )
            r = session.post(device_base_url, data=json.dumps(payload))
            if r.status_code != 200:
                logger.error('Failed to register device: %s', r.text)
                sys.exit(-1)
            logger.info('Device registered: %s', device_id)
            pathlib.Path(os.path.dirname(device_config)).mkdir(exist_ok=True)
            with open(device_config, 'w') as f:
                json.dump(payload, f)

    device_handler = device_helpers.DeviceRequestHandler(device_id)

    @device_handler.command('action.devices.commands.BrightnessAbsolute')
    def brightness_absolute(brightness):
        # ex. 明るさを65%にして
        logger.info('Setting the brightness to %i' % brightness)

    @device_handler.command('action.devices.commands.ColorAbsolute')
    def color_absolute(color):
        # ex. "明かりの色を青くして", "明かりを柔らかい白にして"
        logger.info('Setting the color to %s' % color)

    @device_handler.command('action.devices.commands.Dock')
    def dock():
        # ex. "充電のために戻って"
        logger.info('Returing for charging')

    @device_handler.command('action.devices.commands.OnOff')
    def onoff(on):
        # ex. "つけて","点灯して","消して","消灯して"
        if on:
            subprocess.check_call('./bin/tentou')
            logger.info('Turning device on')
        else:
            subprocess.check_call('./bin/shoutou')
            logger.info('Turning device off')

    @device_handler.command('action.devices.commands.StartStop')
    def startstop(start):
        # ex. "スタートして", "ストップして"
        if start:
            logger.info('Starting device')
        else:
            logger.info('Stopping device')

    @device_handler.command('action.devices.commands.PauseUnpause')
    def pauseunpause(pause):
        # ex. "一時停止して","一時停止を解除して"
        if pause:
            logger.info('Setting pause')
        else:
            logger.info('Unsetting pause')

    @device_handler.command('action.devices.commands.ThermostatTemperatureSetpoint')
    def thermostat(thermostatTemperatureSetpoint):
        logger.info('Setting thermostat to %i' % thermostatTemperatureSetpoint)

    @device_handler.command('io.github.naohirotamura.commands.ReportLightSensor')
    def light_sensor():
        if GPIO_FLAG:
            ratio = lightsensor.read_lightsensor_adc_ratio()
        else:
            ratio = 0
        logger.info('Reporting light sensor AD converter ratio: %.2f percent'
              % ratio)
        synthesize_text.synthesize_text(
            'ライトセンサー AD コンバーター比は %.2f パーセントです' % ratio)

    @device_handler.command('io.github.naohirotamura.commands.ReportHumidity')
    def humidity():
        for i in range(20):
            if GPIO_FLAG:
                result = dht11.read_dht11_dat()
            else:
                result = [0, 0]
            if result:
                break
            else:
                logger.info("%s: Data not good, skip" % i)
                time.sleep(0.5)
        if result:
            humidity, temperature = result
            logger.info("Reporting humidity: %s %%,  Temperature: %s C"
                        % (humidity, temperature))
            synthesize_text.synthesize_text(
                '湿度は %s パーセントです' % humidity)
        else:
            logger.info('Reporting humidity: timeout')
            synthesize_text.synthesize_text(
                '湿度の取得はタイムアウトしました')

    @device_handler.command('io.github.naohirotamura.commands.ReportAltitude')
    def altitude():
        if GPIO_FLAG:
            altitude = bmp.read_altitude()
        else:
            altitude = 0
        logger.info('Reporting altitude: %.2f meter' % altitude)
        synthesize_text.synthesize_text(
            '標高は %.2f メートルです' % altitude)

    @device_handler.command('io.github.naohirotamura.commands.ReportTemperature')
    def temperature():
        if GPIO_FLAG:
            temperature = bmp.read_temperature()
        else:
            temperature = 0
        logger.info('Reporting room temperature: %.2f C' % temperature)
        synthesize_text.synthesize_text(
            '部屋の気温は %.2f 度です' % temperature)

    @device_handler.command('io.github.naohirotamura.commands.ReportPressure')
    def pressure():
        if GPIO_FLAG:
            pressure = bmp.read_pressure() / 100.0
        else:
            pressure = 0
        logger.info('Reporting pressure: %.2f hPa' % pressure)
        synthesize_text.synthesize_text(
            '部屋の気圧は %.2f ヘクトパスカルです' % pressure)

    @device_handler.command('com.fujitsu.commands.CommitCountReport')
    def commit_count_report(repository, start, end):
        OWNER = {'faasshell': 'naohirotamura',
                 'buildah': 'containers',
                 'kubernetes': 'kubernetes'}

        date_pattern = re.compile('(\d{4})年(\d{1,2})月(\d{1,2})日')

        logger.info('Querying ' + repository + ' from ' + start + ' to ' + end)
        if start == '':
            start_iso = date.today().strftime("%Y-%m-%d") + "T00:00:00+00:00"
        else:
            y1,m1,d1 = date_pattern.search(start).groups()
            start_iso = date(int(y1),int(m1),int(d1)).strftime("%Y-%m-%d") + "T00:00:00+00:00"
        if end == '':
            end_iso = (date.today() - timedelta(days=30)).strftime("%Y-%m-%d") + "T00:00:00+00:00"
        else:
            y2,m2,d2 = date_pattern.search(end).groups()
            end_iso = date(int(y2),int(m2),int(d2)).strftime("%Y-%m-%d") + "T00:00:00+00:00"

        print('owner:', OWNER[repository], 'start:', start_iso, 'end:', end_iso)
        result = faasshell.commit_count_report(OWNER[repository], repository,
                                               start_iso, end_iso)
        if 'error' in result.keys():
            logger.info('Commit count report returned error', result['error'])
            synthesize_text.synthesize_text(
                'コミットカウントレポートはエラーになりました')
        else:
            report = result['output']['github']['output']['values'][0]
            logger.info('Commit count report returned ', report)
            synthesize_text.synthesize_text(
                'コミットカウントレポートによると、リポジトリ'
                + report[2] + ' へ' + report[0] + 'は、コミット数'
                + str(report[5]) + ' 件の貢献をしました')


    with SampleAssistant(lang, device_model_id, device_id,
                         conversation_stream, display,
                         grpc_channel, grpc_deadline,
                         device_handler) as assistant:
        # If file arguments are supplied:
        # exit after the first turn of the conversation.
        if input_audio_file or output_audio_file:
            assistant.assist()
            return

        # If no file arguments supplied:
        # keep recording voice requests using the microphone
        # and playing back assistant response using the speaker.
        # When the once flag is set, don't wait for a trigger. Otherwise, wait.
        def detected_callback():
            logger.info("hotword detected")
            snowboywave.play_audio_file(snowboywave.DETECT_DING)
            if GPIO_FLAG:
                LED23.on()
            assistant.assist()
            snowboywave.play_audio_file(snowboywave.DETECT_DONG)
            if GPIO_FLAG:
                LED23.off()

        if once:
            assistant.assist()
        else:
            detector = snowboydecoder.HotwordDetector("resources/snowboy.umdl", sensitivity=0.9, audio_gain=1)
            detector.start(detected_callback)

        """
        wait_for_user_trigger = not once
        while True:
            if wait_for_user_trigger:
                click.pause(info='Press Enter to send a new request...')
            continue_conversation = assistant.assist()
            # wait for user trigger if there is no follow-up turn in
            # the conversation.
            wait_for_user_trigger = not continue_conversation

            # If we only want one conversation, break.
            if once and (not continue_conversation):
                break
        """


if __name__ == '__main__':
    main()
