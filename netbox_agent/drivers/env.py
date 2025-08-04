import re
import os


def get(value, regex):
    env_value = os.environ.get(value, "")
    r = re.search(regex, env_value)
    if r and len(r.groups()) > 0:
        return r.groups()[0]
    return None
