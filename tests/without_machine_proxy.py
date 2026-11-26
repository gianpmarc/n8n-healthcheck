"""Imported by the tests that reach a local stand-in server through the tool's own code.

The requests to the n8n URL use the proxy settings of the machine (docs/what-it-does.md
says so). The opener that makes them is built when `collectors/http.py` is imported, so
on a machine with `HTTP_PROXY` set, or a system proxy, those tests would send their
requests to that proxy instead of to their stand-in on 127.0.0.1, and fail for a reason
that has nothing to do with the tool. Importing this module gives the collector an
opener with the same redirect rule and no proxy, for the length of the test run. Nothing
of the environment is changed, and no collector is.
"""

import urllib.request

from n8n_healthcheck.collectors import http

http._open = urllib.request.build_opener(urllib.request.ProxyHandler({}), http._HttpOnlyRedirects).open
