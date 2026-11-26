"""Imported by the tests that reach a local stand-in server through the tool's own code.

The requests to the n8n URL use the proxy settings of the machine (docs/what-it-does.md
says so). The opener that makes them is built when `collectors/http.py` is imported, so
on a machine with `HTTP_PROXY` set, or a system proxy, those tests would send their
requests to that proxy instead of to their stand-in on 127.0.0.1, and fail for a reason
that has nothing to do with the tool. Importing this module gives the collector, for
the length of the test run, an opener built from the one it ships: every handler that
`collectors/http.py` defines itself (today, the redirect rule), and no proxy. Nothing is
named here, so a rule the collector drops is dropped in the tests too, and the tests
that hold it fail. Nothing of the environment is changed, and no collector is.
"""

import urllib.request

from n8n_healthcheck.collectors import http

_shipped = [type(handler) for handler in http._open.__self__.handlers if type(handler).__module__ == http.__name__]
http._open = urllib.request.build_opener(urllib.request.ProxyHandler({}), *_shipped).open
