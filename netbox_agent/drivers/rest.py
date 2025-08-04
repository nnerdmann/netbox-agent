import re
import socket
import requests


def get(value, regex):
    response = requests.get(f"{value}", timeout=5)
    if response.status_code == 200:
        output = response.text
        r = re.search(regex, output)
        if r and len(r.groups()) > 0:
            return r.groups()[0]
    return None
