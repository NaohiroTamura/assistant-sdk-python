#!/usr/bin/env python

import json
import requests

def commit_count_report():
    faasshell_apihost = 'https://protected-depths-49487.herokuapp.com'
    url = faasshell_apihost + '/statemachine/commit_count_report.json?blocking=true'
    header = {'Content-type': 'application/json'}
    payload = {
        'input': {
            'github': {
                'target': 'fujitsu.com',
                'owner': 'naohirotamura',
                'name': 'faasshell',
                'since': '2018-06-21T00:00:00+00:00',
                'until': '2018-07-20T00:00:00+00:00'
            },
            'gsheet': {
                'sheetId': '1ywCxG8xTKOYK89AEZIqgpTvbvpbrb1s4H_bMVvKV59I'
            }
        }
    }
    auth = requests.auth.HTTPBasicAuth(
        'ec29e90c-188d-11e8-bb72-00163ec1cd01',
        '0b82fe63b6bd450519ade02c3cb8f77ee581f25a810db28f3910e6cdd9d041bf')
    reply = requests.post(url, data=json.dumps(payload), headers=header,
                          verify=False, timeout=30, auth=auth).json()

    return reply


if __name__ == '__main__':
    r = commit_count_report()
    if 'error' in r.keys():
        print(r['error'])
    else:
        print(r['output']['github']['output']['values'][0])
