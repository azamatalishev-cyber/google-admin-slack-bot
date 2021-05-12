from flask import Flask, request, Response, session
import flask
import duo_client
import os
from os.path import join, dirname
from slack_sdk import WebClient
import requests
from dotenv import load_dotenv
from threading import Thread
from google.oauth2 import service_account
import googleapiclient.discovery
import googleapiclient.errors
import logging
import time
from datetime import date
from apiclient.http import MediaFileUpload
from pprint import pprint
import hmac
import hashlib
import time

# Load secrets from '.env' file
dotenv_path = '/credentials/.env'
load_dotenv(dotenv_path)

SCOPE = ['https://www.googleapis.com/auth/drive.file',
         'https://www.googleapis.com/auth/admin.directory.user',
         'https://www.googleapis.com/auth/admin.directory.group.readonly']
ORG_UNIT_PATH = {'orgUnitPath': '/Alumni'}
SUSPENDED_TRUE = {'suspended': 'True'}
SUSPENDED_FALSE = {'suspended': 'False'}
SLACK_RESPONSE = {'text': 'Please approve the duo push and your request will '
                          'be processed'}

HELP = {"attachments": [
    {
        "mrkdwn_in": ["text"],
        "color": "#4b36a6",
        "pretext": "These are the available Google commands:",
        "text": " `/google suspend [email]` Suspend John Doe\n "
                "`/google unsuspend [email]` Unsuspend John Doe\n "
                "`/google offboard [email]` Offboard John Doe "
    }
]
}


file_metadata = {
    'name': 'it_slack_bot_{}.log'.format(date.today().strftime("%m/%d/%y")),
    'mimeType': 'text/plain',
    'parents': [f"{os.environ.get('DRIVE_FOLDER_ID')}"]}

COMMANDS = ['suspend', 'unsuspend', 'offboard', 'help']

keys = service_account.Credentials.from_service_account_file(
    os.environ.get('SERVICE_ACCOUNT_SECRETS_FILE_PATH'),
    scopes=SCOPE, subject=os.environ.get('ADMIN_ACCOUNT'))

google_driver = googleapiclient.discovery.build('admin', 'directory_v1',
                                                cache_discovery=False,
                                                credentials=keys)

drive_client = googleapiclient.discovery.build('drive', 'v3',
                                               cache_discovery=False,
                                               credentials=keys)

slack_client = WebClient(token=os.environ.get('SLACK_TOKEN'))

duo_driver = duo_client.Auth(ikey=os.environ.get('DUO_IKEY'),
                             skey=os.environ.get('DUO_SKEY'),
                             host=os.environ.get('DUO_HOST'))
app = Flask(__name__)

logHandler = logging.FileHandler('app.log')

logHandler.setFormatter(logging.Formatter(
    'Time:[%(asctime)s] Level:[%(levelname)s] %(message)s'))
app.logger.addHandler(logHandler)
app.logger.setLevel(logging.INFO)


@app.route("/google", methods=['POST'])
def get_slash_command_data():
    if not verify_request(request):
        log(f"Client IP:{request.headers.getlist('X-Forwarded-For')} "
            f"{request.form['user']} could not verify request from Slack",
            'error')
        return Response(), 401
    data = request.form
    # Separate command and user argument
    key_arguments = list(data['text'].split(" "))

    # Check arguments
    if not verify_data(key_arguments, data['response_url'], data['user_name']):
        return Response(), 200

    # Compile dictionary of parameters for google action function
    args = {"user": data['user_name'],
            "user_to_update": key_arguments[1],
            "url": data['response_url'],
            "source_ip": request.headers.getlist('X-Forwarded-For'),
            "action": get_action(key_arguments[0])}

    requests.post(data['response_url'], json=SLACK_RESPONSE)

    # Start thread to execute Google API actions
    thread = Thread(target=google_action,
                    args=(args,))
    thread.daemon = True
    thread.start()

    return Response(), 200


def google_action(args):
    cycle_log()
    log(f"Client IP:{args['source_ip']} {args['user']} "
        f"attempting Duo Auth", 'info')
    # Prompt user for 2FA
    resp = duo_driver.auth(username=f"{args['user']}",
                           factor='auto',
                           device='auto')
    # Check 2FA results
    if resp['result'] != 'allow':
        requests.post(args['url'], json={'text': resp['status_msg']})
        log(f"Client IP:{args['source_ip']} {args['user']} attempted to "
            f"authorize with duo to update "
            f"{args['user_to_update']} with {args['action']} - Duo Error: "
            f"{resp['status_msg']}", 'error')
        return

    # Execute Google API action
    google_driver.users().update(userKey=f"{args['user_to_update']}",
                                 body=args['action']).execute()

    log(f"Client IP:{args['source_ip']} {args['user']} updated "
        f"{args['user_to_update']} with {args['action']}", 'info')


def get_action(command):
    actions = {'suspend': SUSPENDED_TRUE, 'unsuspend': SUSPENDED_FALSE,
               'offboard': ORG_UNIT_PATH}
    return actions.get(command)


def verify_request(request):
    if 'X-Slack-Request-Timestamp' not in request.headers or 'X-Slack-Signature' not in request.headers:
        return False
    SIGNING_SECRET = os.environ.get('SLACK_SIGNING_SECRET')
    slack_signing_secret = bytes(SIGNING_SECRET, "utf-8")
    slack_request_timestamp = request.headers["X-Slack-Request-Timestamp"]
    slack_signature = request.headers["X-Slack-Signature"]
    basestring = f"v0:{slack_request_timestamp}:{request.get_data().decode()}"\
        .encode("utf-8")
    my_signature = (
            "v0=" + hmac.new(slack_signing_secret, basestring,
                             hashlib.sha256).hexdigest()
    )
    if hmac.compare_digest(my_signature, slack_signature):
        return True
    return False


def verify_data(data, response_url, user):
    # Check if command exists,provided,
    if not data[0]:
        requests.post(response_url, json=HELP)
        return False
    if data[0] not in COMMANDS:
        requests.post(response_url,
                      json={'text': f'"{data[0]}" is not a valid argument'})
        return False
    if data[0] in 'help':
        requests.post(response_url, json=HELP)
        return False
    if data[0] != 'unsuspend':
        if not check_access(user):
            app.logger.error(f'Insufficient permissions: {user} '
                             f'attempted to {data[0]} {data[1]}')
            requests.post(response_url, json={'text': f'Insufficient '
                                                      f'Permissions to execute'
                                                      f' {data[0]}'})
            return False
    # Check if user exists and/or if user argument was provided
    try:
        google_driver.users().get(userKey=data[1]).execute()

    except IndexError:
        requests.post(response_url,
                      json={'text': 'No user argument provided'})
        return False

    except googleapiclient.errors.HttpError:
        requests.post(response_url,
                      json={'text': f'"{data[1]}" user not found'})
        return False

    return True


def log(text, message_type):
    if message_type == 'error':
        app.logger.error(text)
        slack_client.chat_postMessage(
            channel=f"{os.environ.get('SLACK_CHANNEL_ID')}", text=text)
        return

    app.logger.info(text)
    slack_client.chat_postMessage(
        channel=f"{os.environ.get('SLACK_CHANNEL_ID')}", text=text)


def cycle_log():
    size = os.path.getsize(os.environ.get('LOG_PATH'))

    if size < int(os.environ.get('MAX_LOG_SIZE')):
        return

    media = MediaFileUpload(os.environ.get('LOG_PATH'), mimetype='text/plain')
    drive_client.files().create(body=file_metadata, media_body=media,
                                supportsAllDrives=True).execute()

    file = open(os.environ.get('LOG_PATH'), 'r+')
    file.truncate(0)
    file.close()


def check_access(user_updating):
    resp = google_driver.members().list(
        groupKey='it@greenhouse.io').execute()

    emails = [user['email'] for user in resp['members']]

    if user_updating + '@greenhouse.io' not in emails:
        return False

    return True


@app.route("/testing", methods=['GET'])
def test():
    data = request.json
    print(data)
    cycle_log()
    log_info = request.headers.getlist('X-Forwarded-For')
    app.logger.info(f"SOURCE IP = {log_info} This is a test ")
    return Response(), 200


if __name__ == "__main__":
    app.run(debug=True, port=5000)
