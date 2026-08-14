"""
hazop.authgate — optional HTTP Basic password gate for the subsystem web apps.

Inert unless HAZOP_WEB_PASSWORD is set in the environment, so local runs
(`python -m hazop.s5_sw.app`) behave exactly as before.  The tunnel wrapper
(`share.sh`) sets it, which is what keeps a public *.trycloudflare.com URL
from being an open door onto the study data.

    from hazop.authgate import install_password_gate
    install_password_gate(app)
"""

import os
from hmac import compare_digest

from flask import Response, request

ENV_VAR = "HAZOP_WEB_PASSWORD"


def install_password_gate(app, realm: str = "HAZOP-AI") -> bool:
    """Attach the gate to `app`.  Returns True if it is actually armed."""
    expected = os.environ.get(ENV_VAR)
    if not expected:
        return False

    @app.before_request
    def _require_password():
        auth = request.authorization
        # compare_digest over the password only — the username is unused, so
        # any username paired with the right password gets in.
        if auth and compare_digest(auth.password or "", expected):
            return None
        return Response(
            "authentication required\n", 401,
            {"WWW-Authenticate": f'Basic realm="{realm}"'},
        )

    return True
