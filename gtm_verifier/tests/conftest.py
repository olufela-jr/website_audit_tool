"""Put the gtm_verifier package directory on sys.path so the audit modules
(seo, security_headers, httpfetch, core, config) import when pytest runs from
anywhere."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
